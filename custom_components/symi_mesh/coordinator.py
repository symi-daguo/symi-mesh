"""Coordinator for the Symi Mesh gateway integration.

Owns the transport, the device registry and the push path from mesh events to
HA entities. Supports dynamic entity creation when the gateway reports new
devices at runtime.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo

from .const import (
    DOMAIN,
    KIND_GATEWAY,
    OP_EVENT_NODE_STATUS,
    OP_RESP_DEVICE_LIST,
)
from .device import DeviceInfo as MeshDevice, DeviceRegistry
from .gateway import GatewayTransport
from .protocol import parse_device_list_frame, parse_status_event

_LOGGER = logging.getLogger(__name__)

PLATFORMS = (
    "switch",
    "light",
    "cover",
    "climate",
    "fan",
    "sensor",
    "binary_sensor",
    "scene",
)

# How long to wait for the full device list after the first 0x92 frame.
DEVICE_LIST_TIMEOUT_S = 10.0


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
        self._promoted_three_in_one: set[str] = set()

    # -- lifecycle ----------------------------------------------------------

    async def async_setup(self) -> None:
        data = {**self.entry.data, **self.entry.options}
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
        self._start_task = self.hass.async_create_task(self.transport.async_start())
        _LOGGER.info(
            "Symi Mesh coordinator starting (%s)",
            f"{data.get('host')}:{data.get('port')}" if connection_type == "tcp" else data.get("serial_port"),
        )

    async def async_shutdown(self) -> None:
        self._started = False
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
            self._handle_status_event(frame)
        # 0xB0 / 0xB2 / 0xB4 are gateway ACK markers — data arrives via 0x80.

    def _handle_device_list_frame(self, frame: Any) -> None:
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
        # Wait for index == max-1 (or timeout) before the first state query.
        if parsed["index"] >= parsed["maxDevices"] - 1 and not self._device_list_done.is_set():
            if self._device_list_deadline is not None:
                self._device_list_deadline.cancel()
            self._device_list_done.set()
            self.hass.async_create_task(self._after_device_list())

    def _handle_status_event(self, frame: Any) -> None:
        try:
            event = parse_status_event(frame)
        except ValueError:
            return
        device = self.registry.get_by_address(event["networkAddress"])
        if device is None:
            return
        was_three_in_one = device.is_three_in_one
        device.update_from_event(event["attrType"], event["parameters"])
        self._notify_update(device.mac_address)
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

        # First-run state query: detects three-in-one panels and seeds states.
        for device in devices:
            try:
                await self.transport.async_send_frame(
                    self.transport.protocol.build_device_status_query_frame(device.network_address)
                )
                if device.device_type == 10:
                    # Probe fresh-air / floor-heating attrs to classify panels.
                    for msg_type in (0x68, 0x6B):
                        await self.transport.async_send_frame(
                            self.transport.protocol.build_device_status_query_frame(
                                device.network_address, msg_type
                            )
                        )
            except Exception:  # noqa: BLE001
                _LOGGER.debug("Failed to query %s", device.mac_address)

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

    async def async_set_switch(
        self, device: MeshDevice, channel: int, turn_on: bool
    ) -> None:
        assert self.transport is not None
        frame = self.transport.protocol.build_switch_control_frame(
            device.network_address,
            device.channels,
            channel,
            turn_on,
            device.get_current_switch_state(),
        )
        await self.transport.async_send_frame(frame)

    async def async_trigger_scene(self, scene_id: int) -> None:
        assert self.transport is not None
        frame = self.transport.protocol.build_scene_control_frame(scene_id)
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

    def gateway_device_info(self) -> DeviceInfo:
        data = {**self.entry.data, **self.entry.options}
        if data.get("connection_type") == "serial":
            identifier = f"serial:{data.get('serial_port')}"
            name = f"Symi Mesh 网关 ({data.get('serial_port')})"
        else:
            identifier = f"tcp:{data.get('host')}:{data.get('port')}"
            name = f"Symi Mesh 网关 ({data.get('host')})"
        return DeviceInfo(
            identifiers={(DOMAIN, identifier)},
            name=name,
            manufacturer="SYMI 亖米",
            model="Symi Mesh 网关",
        )

    @staticmethod
    def gateway_identifier(entry: Any) -> str:
        data = {**entry.data, **entry.options}
        if data.get("connection_type") == "serial":
            return f"serial:{data.get('serial_port')}"
        return f"tcp:{data.get('host')}:{data.get('port')}"

    @property
    def is_gateway_entry(self) -> bool:
        return self.entry.data.get("kind") == KIND_GATEWAY
