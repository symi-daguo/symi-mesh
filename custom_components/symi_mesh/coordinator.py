"""Coordinator for the Symi Mesh gateway integration.

Owns the transport, the device registry and the push path from mesh events to
HA entities. Supports dynamic entity creation when the gateway reports new
devices at runtime.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Callable

from homeassistant.core import HomeAssistant, callback

from .const import (
    CONF_QUERY_ON_STARTUP,
    DOMAIN,
    EVENT_LIST_NODE_UPDATE_RPT,
    EVENT_NET_SCENE_ACT,
    KIND_GATEWAY,
    OP_EVENT_NODE_STATUS,
    OP_RESP_DEVICE_LIST,
)
from .device import DeviceInfo as MeshDevice, DeviceRegistry
from .gateway import GatewayTransport
from .protocol import parse_device_list_frame, parse_status_event, parse_status_pairs

_LOGGER = logging.getLogger(__name__)

PLATFORMS = (
    "switch",
    "light",
    "cover",
    "climate",
    "fan",
    "scene",
    "sensor",
    "binary_sensor",
    "lock",
)

# How long to wait for the full device list after the first 0x92 frame.
DEVICE_LIST_TIMEOUT_S = 10.0
# Startup status-query retry (protocol doc §3.1: 200 ms, max 3 attempts).
QUERY_TIMEOUT_S = 0.2
QUERY_MAX_ATTEMPTS = 3


class SymiMeshCoordinator:
    """Gateway connection + device registry + entity push."""

    def __init__(self, hass: HomeAssistant, entry: Any) -> None:
        self.hass = hass
        self.entry = entry
        self.registry = DeviceRegistry()
        self.transport: GatewayTransport | None = None
        self._start_task: asyncio.Task | None = None
        self._platform_adders: dict[str, Callable[[list], None]] = {}
        self._update_listeners: list[Callable[[str], None]] = []
        self._device_list_done = asyncio.Event()
        self._device_list_deadline: asyncio.Task | None = None
        self._started = False
        self._added_entity_ids: set[str] = set()
        self._scene_entities: dict[tuple[str, int], Any] = {}
        self._promoted_three_in_one: set[str] = set()
        self._pending_queries: dict[tuple[int, int], int] = {}
        self._query_waiters: dict[tuple[int, int], asyncio.Future] = {}
        self._gateway_device_id: str | None = None

    @property
    def gateway_device_id(self) -> str | None:
        """Device registry id of the gateway device (for via_device_id)."""
        return self._gateway_device_id

    def _ensure_gateway_device(self) -> None:
        """Register the gateway device in the device registry once."""
        if self._gateway_device_id is not None:
            return
        from homeassistant.helpers import device_registry as dr

        dev_reg = dr.async_get(self.hass)
        identifier = self.gateway_identifier(self.entry)
        data = {**self.entry.data, **self.entry.options}
        if data.get("connection_type") == "serial":
            name = f"Symi Mesh 网关 ({data.get('serial_port')})"
        else:
            name = f"Symi Mesh 网关 ({data.get('host')})"
        device = dev_reg.async_get_or_create(
            config_entry_id=self.entry.entry_id,
            identifiers={(DOMAIN, identifier)},
            name=name,
            manufacturer="SYMI 亖米",
            model="Symi Mesh 网关",
        )
        self._gateway_device_id = device.id

    # -- lifecycle ----------------------------------------------------------

    async def async_setup(self) -> None:
        data = {**self.entry.data, **self.entry.options}
        self._query_on_startup = bool(data.get(CONF_QUERY_ON_STARTUP, True))
        self._ensure_gateway_device()
        connection_type = data.get("connection_type", "tcp")
        self.transport = GatewayTransport(
            connection_type=connection_type,
            host=data.get("host"),
            port=int(data.get("port", 4196)),
            serial_port=data.get("serial_port"),
            baud_rate=int(data.get("baud_rate", 115200)),
            frame_interval=0.05,
            reconnect_interval=int(data.get("reconnect_interval", 5)),
        )
        self.transport.set_frame_callback(self._handle_frame)
        self.transport.set_connection_callbacks(self._on_connected, self._on_disconnected)
        self._started = True
        # Background task so a blocked/retrying connection never stalls HA
        # startup wrap-up (HA >= 2025.4) — fall back to async_create_task.
        create = getattr(self.hass, "async_create_background_task", None)
        if create is not None:
            self._start_task = create(self.transport.async_start(), name="symi_mesh_transport")
        else:  # pragma: no cover - HA < 2025.4
            self._start_task = self.hass.async_create_task(self.transport.async_start())
        _LOGGER.info(
            "Symi Mesh coordinator starting (%s)",
            f"{data.get('host')}:{data.get('port')}" if connection_type == "tcp" else data.get("serial_port"),
        )

    async def async_shutdown(self) -> None:
        self._started = False
        for waiter in list(self._query_waiters.values()):
            if not waiter.done():
                waiter.cancel()
        self._query_waiters.clear()
        self._pending_queries.clear()
        if self.transport is not None:
            await self.transport.async_stop()
        if self._start_task is not None:
            self._start_task.cancel()
        self._update_listeners.clear()
        self._platform_adders.clear()

    # -- platform registration ----------------------------------------------

    def register_platform(self, platform: str, add_entities: Callable[[list], None]) -> None:
        """Platforms register their add_entities callback here."""
        self._platform_adders[platform] = add_entities
        if platform == "scene":
            self.restore_scene_entities(add_entities)
        # Catch up with devices discovered before the platform loaded.
        for device in self.registry.all_devices():
            entities = [
                e
                for e in self._create_entities_for_device(platform, device)
                if e.unique_id not in self._added_entity_ids
            ]
            if entities:
                for entity in entities:
                    self._added_entity_ids.add(entity.unique_id)
                add_entities(entities)

    def add_update_listener(self, listener: Callable[[str], None]) -> Callable[[], None]:
        """Subscribe to device updates; returns an unsubscribe callable."""
        self._update_listeners.append(listener)

        def _remove() -> None:
            if listener in self._update_listeners:
                self._update_listeners.remove(listener)

        return _remove

    @callback
    def _notify_update(self, mac: str) -> None:
        for listener in list(self._update_listeners):
            try:
                listener(mac)
            except Exception:  # noqa: BLE001 - listener isolation
                _LOGGER.exception("Update listener failed for %s", mac)

    # -- connection events ---------------------------------------------------

    async def _on_connected(self) -> None:
        _LOGGER.info("Gateway connected; requesting device list")
        await self._request_device_list()

    async def _on_disconnected(self) -> None:
        _LOGGER.warning("Gateway disconnected")
        for device in self.registry.all_devices():
            device.online = False
            self._notify_update(device.mac_address)

    async def _request_device_list(self) -> None:
        assert self.transport is not None
        self._device_list_done.clear()
        await self.transport.async_send_frame(
            self.transport.protocol.build_read_device_list_frame(), priority=0
        )
        if self._device_list_deadline is not None:
            self._device_list_deadline.cancel()
        self._device_list_deadline = self.hass.loop.call_later(
            DEVICE_LIST_TIMEOUT_S, self._finish_device_list
        )

    def _finish_device_list(self) -> None:
        if not self._device_list_done.is_set():
            _LOGGER.warning(
                "Device list timeout; proceeding with %d known devices",
                len(self.registry.all_devices()),
            )
            self._device_list_done.set()

    # -- frame handling -------------------------------------------------------

    @callback
    def _handle_frame(self, frame: Any) -> None:
        opcode = frame.opcode
        if opcode == OP_RESP_DEVICE_LIST:
            self._handle_device_list_frame(frame)
        elif opcode == OP_EVENT_NODE_STATUS:
            if frame.status == EVENT_LIST_NODE_UPDATE_RPT:
                # Node-list update event: same payload as a device-list frame.
                self._handle_device_list_frame(frame, is_update=True)
            elif frame.status == EVENT_NET_SCENE_ACT:
                # 网络场景执行通知（payload=[addr_lo addr_hi scene_no]）——
                # 物理按键（无网关场景/面板）触发场景时上报，用于日志可见场景号 + HA 事件
                self._handle_scene_notify(frame)
            else:
                self._handle_status_event(frame)
        # 0xB0 / 0xB2 / 0xB4 are gateway ACK markers — data arrives via 0x80.

    def _handle_device_list_frame(self, frame: Any, is_update: bool = False) -> None:
        parsed = parse_device_list_frame(frame)
        if parsed is None:
            return
        device, is_new = self.registry.add_or_update(parsed)
        if is_new:
            _LOGGER.info(
                "Discovered device %s (%s) type=0x%02X @ 0x%04X",
                device.name,
                device.mac_address,
                device.device_type,
                device.network_address,
            )
            self._add_entities_for_new_device(device)
        if is_update:
            # Runtime list-update event: no discovery bookkeeping.
            return
        # Wait for index == max-1 (or timeout) before the first state query.
        if parsed["index"] >= parsed["maxDevices"] - 1 and not self._device_list_done.is_set():
            if self._device_list_deadline is not None:
                self._device_list_deadline.cancel()
            self._device_list_done.set()
            self.hass.async_create_task(self._after_device_list())

    def restore_scene_entities(self, add_entities: Any) -> None:
        """scene 平台注册后，从实体注册表恢复已知场景实体（防重启重复生成，保留改名）。"""
        import homeassistant.helpers.entity_registry as er

        registry = er.async_get(self.hass)
        known: list[Any] = []
        from .scene import SymiSceneEntity

        for entry in registry.entities.values():
            uid = entry.unique_id or ""
            if not uid.startswith("symi_mesh_scene_") or entry.domain != "scene":
                continue
            rest = uid[len("symi_mesh_scene_"):]
            if "_" not in rest:
                continue
            mac, _, sid = rest.rpartition("_")
            try:
                scene_no = int(sid)
            except ValueError:
                continue
            device = self.registry.get_by_mac(mac)
            if device is None:
                continue  # 非本网关设备，跳过（避免多网关重复恢复）
            name = device.name
            entity = SymiSceneEntity(self, mac, name, scene_no)
            self._scene_entities[(mac, scene_no)] = entity
            known.append(entity)
        if known:
            _LOGGER.info("恢复场景实体 %d 个", len(known))
            add_entities(known)

    def _handle_scene_notify(self, frame: Any) -> None:
        """网络场景执行通知（0x11）：动态生成/触发对应的 HA scene 实体。

        每个「设备 + 场景号」生成一个 scene 实体（接收方），供 HA 自动化
        绑定 KNX 场景使用；触发时更新实体状态 + fire 事件。
        """
        payload = frame.payload
        if len(payload) < 3:
            return
        addr = payload[0] | (payload[1] << 8)
        scene_no = payload[2]
        device = self.registry.get_by_address(addr)
        mac = device.mac_address if device is not None else f"0x{addr:04X}"
        name = device.name if device is not None else ""
        _LOGGER.info("网络场景执行: 设备 %s(%s) -> 场景 %d", name, mac, scene_no)
        self.hass.bus.async_fire(
            "symi_mesh_scene_event",
            {"mac": mac, "name": name, "scene_id": scene_no},
        )

        key = (mac, scene_no)
        entity = self._scene_entities.get(key)
        if entity is not None:
            entity.notify_scene()
            return
        adder = self._platform_adders.get("scene")
        if adder is None:
            return  # scene 平台尚未注册，下次触发再建
        from .scene import SymiSceneEntity

        entity = SymiSceneEntity(self, mac, name, scene_no)
        self._scene_entities[key] = entity
        self._added_entity_ids.add(entity.unique_id)
        adder([entity])
        entity.notify_scene()

    def _handle_status_event(self, frame: Any) -> None:
        try:
            event = parse_status_event(frame)
        except ValueError:
            return
        network_address = event["networkAddress"]
        device = self.registry.get_by_address(network_address)
        if device is None:
            return

        # One event may carry several (msg_type, param) pairs.
        pairs = parse_status_pairs(frame.payload[2:])
        was_three_in_one = device.is_three_in_one
        device.update_from_pairs(pairs)
        self._notify_update(device.mac_address)

        # SCENE_PANEL_EVENT（0x07）：场景按键触发 → fire HA 事件（automation 可绑定
        # knx.send 等 KNX 场景联动）；只在新触发时派发，天然防回环。
        triggers = device.state.get("sceneTriggers")
        if triggers is not None:
            prev = getattr(device, "_last_scene_triggers", None)
            device._last_scene_triggers = list(triggers)
            if triggers != prev:
                fired_keys = [i + 1 for i, hit in enumerate(triggers) if hit]
                if fired_keys:
                    self.hass.bus.async_fire(
                        "symi_mesh_scene_event",
                        {"mac": device.mac_address, "name": device.name,
                         "keys": fired_keys},
                    )

        # mesh 面板等外部操作产生的窗帘位置：HA 采纳后必须把该百分比响应回发
        # mesh（0x06 同位置），否则 mesh 端状态错乱。回发后设命令窗口防环。
        echo_pos = device.state.pop("curtainEchoPending", None)
        if echo_pos is not None:
            self.hass.async_create_task(
                self._async_echo_curtain_position(device, int(echo_pos))
            )

        # A response arrived for a pending startup query.
        self._clear_pending_query(network_address)

        # A type-10 panel that answered fresh-air / floor-heating queries is a
        # three-in-one unit: create the extra fan + floor-heating entities.
        if (
            device.device_type == 10
            and device.is_three_in_one
            and not was_three_in_one
            and device.mac_address not in self._promoted_three_in_one
        ):
            self._promoted_three_in_one.add(device.mac_address)
            _LOGGER.info("Device %s identified as three-in-one panel", device.mac_address)
            for platform in ("fan", "climate"):
                adder = self._platform_adders.get(platform)
                if adder is None:
                    continue
                entities = [
                    e
                    for e in self._create_entities_for_device(platform, device)
                    if e.unique_id not in self._added_entity_ids
                ]
                if entities:
                    for entity in entities:
                        self._added_entity_ids.add(entity.unique_id)
                    adder(entities)

    async def _async_echo_curtain_position(self, device: MeshDevice, pos: int) -> None:
        """把 mesh 面板等外部操作的位置百分比响应回发 mesh（0x06 同位置）。"""
        try:
            await self.async_send_control(device, 0x06, bytes([pos]))
            device.state["curtainCmdUntil"] = time.time() + 8
            _LOGGER.info(
                "窗帘位置响应回发: %s(0x%04X) -> %d%%",
                device.name,
                device.network_address,
                pos,
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("窗帘位置响应回发失败: %s -> %d%% (%s)", device.mac_address, pos, err)

    async def _after_device_list(self) -> None:
        """Enable status reporting + first state query (three-in-one probe)."""
        assert self.transport is not None
        await asyncio.sleep(0.5)
        devices = self.registry.all_devices()
        _LOGGER.info("Device list complete: %d devices; enabling status reports", len(devices))
        for device in devices:
            try:
                await self.transport.async_send_frame(
                    self.transport.protocol.build_enable_status_report_frame(device.network_address)
                )
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Failed to enable report for %s", device.mac_address)

        if not self._query_on_startup:
            return

        # First-run state query with protocol-mandated retry (200 ms, 3 tries).
        # Also probes fresh-air / floor-heating attrs to classify panels.
        _LOGGER.info("Starting initial state query for %d devices", len(devices))
        for device in devices:
            await self._query_device_with_retry(device.network_address)
            if device.device_type == 10:
                for msg_type in (0x68, 0x6B):
                    await self._query_device_with_retry(device.network_address, msg_type)
        _LOGGER.info("Initial state query finished")

    async def _query_device_with_retry(
        self, network_address: int, msg_type: int = 0x00
    ) -> None:
        """Query one attribute; resend after 200 ms, max 3 attempts (doc §3.1)."""
        assert self.transport is not None
        key = (network_address, msg_type)
        for attempt in range(1, QUERY_MAX_ATTEMPTS + 1):
            self._pending_queries[key] = attempt
            try:
                await self.transport.async_send_frame(
                    self.transport.protocol.build_device_status_query_frame(
                        network_address, msg_type
                    )
                )
            except Exception:  # noqa: BLE001
                self._pending_queries.pop(key, None)
                return
            if await self._wait_query_response(key):
                return
        self._pending_queries.pop(key, None)
        _LOGGER.debug(
            "No status response for 0x%04X msg_type=0x%02X after %d attempts",
            network_address,
            msg_type,
            QUERY_MAX_ATTEMPTS,
        )

    async def _wait_query_response(self, key: tuple[int, int]) -> bool:
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._query_waiters[key] = future
        try:
            await asyncio.wait_for(future, timeout=QUERY_TIMEOUT_S)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            self._query_waiters.pop(key, None)

    def _clear_pending_query(self, network_address: int) -> None:
        """Resolve waiters for every pending attribute of this device."""
        for key in list(self._pending_queries):
            if key[0] != network_address:
                continue
            self._pending_queries.pop(key, None)
            waiter = self._query_waiters.pop(key, None)
            if waiter is not None and not waiter.done():
                waiter.set_result(True)

    # -- dynamic entities ------------------------------------------------------

    def _create_entities_for_device(self, platform: str, device: MeshDevice) -> list:
        """Build entity descriptions for one device on one platform."""
        from .entity import build_entities_for_platform

        return build_entities_for_platform(platform, device, self)

    def _add_entities_for_new_device(self, device: MeshDevice) -> None:
        for platform, adder in self._platform_adders.items():
            entities = [
                e
                for e in self._create_entities_for_device(platform, device)
                if e.unique_id not in self._added_entity_ids
            ]
            if not entities:
                continue
            for entity in entities:
                self._added_entity_ids.add(entity.unique_id)
            try:
                adder(entities)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Failed to add entities for %s", device.mac_address)

    # -- commands ---------------------------------------------------------------

    async def async_send_control(
        self, device: MeshDevice, attr_type: int, param: bytes = b""
    ) -> None:
        assert self.transport is not None
        frame = self.transport.protocol.build_device_control_frame(
            device.network_address, attr_type, param
        )
        await self.transport.async_send_frame(frame)
        _LOGGER.info(
            "控制下发: %s(0x%04X) attr=0x%02X param=%s",
            device.name,
            device.network_address,
            attr_type,
            param.hex(" ").upper(),
        )

    async def async_set_switch(
        self, device: MeshDevice, channel: int, turn_on: bool
    ) -> None:
        assert self.transport is not None
        # Optimistically update before building the packed state so rapid
        # multi-channel toggles compose the correct current_state.
        device.set_channel_state(channel, turn_on)
        self._notify_update(device.mac_address)
        frame = self.transport.protocol.build_switch_control_frame(
            device.network_address,
            device.channels,
            channel,
            turn_on,
            device.get_current_switch_state(),
        )
        await self.transport.async_send_frame(frame)
        _LOGGER.info(
            "控制下发: %s(0x%04X) attr=0x02 param=%s",
            device.name,
            device.network_address,
            frame[6:-1].hex(" ").upper(),
        )

    async def async_trigger_scene(self, scene_id: int) -> None:
        assert self.transport is not None
        frame = self.transport.protocol.build_scene_control_frame(scene_id)
        await self.transport.async_send_frame(frame)

    async def async_trigger_scene_transparent(self, scene_id: int) -> None:
        """透传节点场景执行（0x34 广播 + 重传 5 次）。"""
        assert self.transport is not None
        frame = self.transport.protocol.build_scene_control_frame_transparent(scene_id)
        await self.transport.async_send_frame(frame)

    async def async_scan_devices(self) -> int:
        """Re-request the device list; returns the current device count."""
        await self._request_device_list()
        try:
            await asyncio.wait_for(self._device_list_done.wait(), timeout=DEVICE_LIST_TIMEOUT_S)
        except asyncio.TimeoutError:
            pass
        return len(self.registry.all_devices())

    # -- device info -----------------------------------------------------------

    @staticmethod
    def gateway_identifier(entry: Any) -> str:
        data = {**entry.data, **entry.options}
        if data.get("connection_type") == "serial":
            return f"serial:{data.get('serial_port')}"
        return f"tcp:{data.get('host')}:{data.get('port')}"

    @property
    def is_gateway_entry(self) -> bool:
        return self.entry.data.get("kind") == KIND_GATEWAY
