"""Symi Mesh — Home Assistant custom integration.

Two config-entry flavours share the ``symi_mesh`` domain:

* ``gateway``   — TCP/serial link to a Symi Mesh gateway; auto-discovers the
                  mesh devices behind it and exposes them as HA entities with
                  MAC-derived unique IDs.
* ``knx_sync``  — a bidirectional sync group between any HA entities (e.g.
                  official KNX integration entities and symi_mesh entities),
                  using the origin-stamped anti-loop engine ported from
                  symi_two_way_sync.

The module top level is free of Home Assistant imports so the protocol and
device layers stay unit-testable without HA installed.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from .const import (
    DOMAIN,
    KIND_GATEWAY,
    KIND_KNX_SYNC,
    SERVICE_DESCRIBE,
    SERVICE_RELOAD,
    SERVICE_TRIGGER_SCENE,
    SERVICE_SCAN_DEVICES,
)
if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .runtime import SymiMeshConfigEntry

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [
    "switch",
    "light",
    "cover",
    "climate",
    "fan",
    "sensor",
    "binary_sensor",
    "scene",
    "lock",
]


async def async_setup_entry(hass: HomeAssistant, entry: SymiMeshConfigEntry) -> bool:
    kind = entry.data.get("kind", KIND_GATEWAY)
    if kind == KIND_KNX_SYNC:
        return await _async_setup_sync_entry(hass, entry)
    return await _async_setup_gateway_entry(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: SymiMeshConfigEntry) -> bool:
    kind = entry.data.get("kind", KIND_GATEWAY)
    if kind == KIND_KNX_SYNC:
        return await _async_unload_sync_entry(hass, entry)
    return await _async_unload_gateway_entry(hass, entry)


# ---------------------------------------------------------------------------
# Gateway entries
# ---------------------------------------------------------------------------


async def _async_setup_gateway_entry(
    hass: HomeAssistant, entry: SymiMeshConfigEntry
) -> bool:
    from homeassistant.exceptions import ConfigEntryNotReady

    from .coordinator import SymiMeshCoordinator
    from .runtime import SymiMeshRuntime

    coordinator = SymiMeshCoordinator(hass, entry)
    entry.runtime_data = SymiMeshRuntime(kind=KIND_GATEWAY, coordinator=coordinator)
    try:
        await coordinator.async_setup()
    except ConfigEntryNotReady:
        entry.runtime_data = SymiMeshRuntime(kind=KIND_GATEWAY)
        raise
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _async_register_services(hass)
    return True


async def _async_unload_gateway_entry(
    hass: HomeAssistant, entry: SymiMeshConfigEntry
) -> bool:
    from .runtime import SymiMeshRuntime

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator = entry.runtime_data.coordinator
    if coordinator is not None:
        await coordinator.async_shutdown()
    entry.runtime_data = SymiMeshRuntime(kind=KIND_GATEWAY)
    _async_maybe_remove_services(hass, entry.entry_id)
    return unload_ok


# ---------------------------------------------------------------------------
# KNX sync entries
# ---------------------------------------------------------------------------


async def _async_setup_sync_entry(
    hass: HomeAssistant, entry: SymiMeshConfigEntry
) -> bool:
    from .const import (
        CONF_SYNC_DIRECTION,
        CONF_SYNC_ENABLED,
        CONF_SYNC_ENTITIES,
        CONF_SYNC_NAME,
        SYNC_DIRECTION_TWO_WAY,
    )
    from .knx_sync import SymiSyncEngine
    from .runtime import SymiMeshRuntime
    from .settings import async_load_settings

    await async_load_settings(hass)
    raw = {**entry.data, **entry.options}
    group = {
        CONF_SYNC_NAME: raw.get(CONF_SYNC_NAME) or entry.title,
        CONF_SYNC_ENTITIES: list(raw.get(CONF_SYNC_ENTITIES) or []),
        CONF_SYNC_DIRECTION: raw.get(CONF_SYNC_DIRECTION, SYNC_DIRECTION_TWO_WAY),
        CONF_SYNC_ENABLED: bool(raw.get(CONF_SYNC_ENABLED, True)),
    }
    engine = SymiSyncEngine(hass, group)
    await engine.async_start()
    entry.runtime_data = SymiMeshRuntime(kind=KIND_KNX_SYNC, engine=engine)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _LOGGER.info("已启用同步组「%s」: %s", group[CONF_SYNC_NAME], group[CONF_SYNC_ENTITIES])
    return True


async def _async_unload_sync_entry(
    hass: HomeAssistant, entry: SymiMeshConfigEntry
) -> bool:
    from .runtime import SymiMeshRuntime

    engine = entry.runtime_data.engine
    if engine is not None:
        await engine.async_stop()
    entry.runtime_data = SymiMeshRuntime(kind=KIND_KNX_SYNC)
    _async_maybe_remove_services(hass, entry.entry_id)
    return True


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


async def _async_update_listener(
    hass: HomeAssistant, entry: SymiMeshConfigEntry
) -> None:
    """Reload on config/options change.

    HA already reloads the entry itself after an options flow completes; our
    listener can then race with that reload and hit UnknownEntry. Guard it so
    a benign race never leaves the engine running with a stale entity list.
    """
    from homeassistant.config_entries import UnknownEntry

    try:
        await hass.config_entries.async_reload(entry.entry_id)
    except UnknownEntry:
        _LOGGER.debug("跳过重载（条目正在被 HA 重载/卸载）: %s", entry.entry_id)


def _runtime(entry: SymiMeshConfigEntry) -> SymiMeshRuntime | None:
    """Runtime data of an entry that is currently loaded (None otherwise)."""
    return getattr(entry, "runtime_data", None)


def _gateway_coordinators(hass: HomeAssistant) -> list[Any]:
    """Coordinators of every loaded gateway entry, in registry order."""
    coordinators = []
    for entry in hass.config_entries.async_entries(DOMAIN):
        runtime = _runtime(entry)
        if runtime is not None and runtime.coordinator is not None:
            coordinators.append(runtime.coordinator)
    return coordinators


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_RELOAD):
        return

    async def _reload_all(_call: Any) -> None:
        for entry in hass.config_entries.async_entries(DOMAIN):
            await hass.config_entries.async_reload(entry.entry_id)

    async def _scan_devices(_call: Any) -> None:
        count = 0
        for coordinator in _gateway_coordinators(hass):
            count = await coordinator.async_scan_devices()
        _LOGGER.info("设备扫描完成，当前设备数: %s", count)

    async def _describe(_call: Any) -> None:
        snaps = []
        for entry in hass.config_entries.async_entries(DOMAIN):
            runtime = _runtime(entry)
            engine = runtime.engine if runtime is not None else None
            if engine is None:
                continue
            snaps.append({"entry_id": entry.entry_id, "snapshot": engine.describe()})
        _LOGGER.info("symi_mesh 状态: %s", snaps)
        hass.bus.async_fire(f"{DOMAIN}_describe", {"entries": snaps})

    async def _trigger_scene(call: Any) -> None:
        """场景控制：0x31 标准群控（默认）或 0x34 透传（公版/离线场景节点）。"""
        scene_id = int(call.data["scene_id"])
        transparent = bool(call.data.get("transparent", False))
        sent = 0
        for coordinator in _gateway_coordinators(hass):
            try:
                if transparent:
                    await coordinator.async_trigger_scene_transparent(scene_id)
                else:
                    await coordinator.async_trigger_scene(scene_id)
                sent += 1
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("场景控制下发失败 scene=%s: %s", scene_id, err)
        _LOGGER.info("场景控制完成: scene=%s transparent=%s 网关数=%s", scene_id, transparent, sent)

    import homeassistant.helpers.config_validation as cv
    import voluptuous as vol

    hass.services.async_register(DOMAIN, SERVICE_RELOAD, _reload_all)
    hass.services.async_register(DOMAIN, SERVICE_SCAN_DEVICES, _scan_devices)
    hass.services.async_register(DOMAIN, SERVICE_DESCRIBE, _describe)
    hass.services.async_register(
        DOMAIN,
        SERVICE_TRIGGER_SCENE,
        _trigger_scene,
        schema=vol.Schema(
            {
                vol.Required("scene_id"): vol.All(vol.Coerce(int), vol.Range(min=1, max=255)),
                vol.Optional("transparent", default=False): cv.boolean,
            }
        ),
    )


def _async_maybe_remove_services(
    hass: HomeAssistant, exclude_entry_id: str | None = None
) -> None:
    """Drop the domain services once the last entry is gone."""
    remaining = [
        entry
        for entry in hass.config_entries.async_entries(DOMAIN)
        if entry.entry_id != exclude_entry_id
    ]
    if remaining:
        return
    for service in (
        SERVICE_RELOAD,
        SERVICE_SCAN_DEVICES,
        SERVICE_DESCRIBE,
        SERVICE_TRIGGER_SCENE,
    ):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: SymiMeshConfigEntry, device_entry
) -> bool:
    """允许用户从设备页面删除无效设备（含其全部实体）。

    网关本体设备不可删（删除即卸载集成，走条目删除）；mesh 子设备一律可删——
    删除后若网关仍在设备列表中上报该设备，会作为新设备重新被发现。
    """
    from .coordinator import SymiMeshCoordinator

    if config_entry.data.get("kind", KIND_GATEWAY) != KIND_GATEWAY:
        return True
    identifiers = device_entry.identifiers or set()
    clean_mac = None
    for domain, value in identifiers:
        if domain != DOMAIN:
            continue
        if value == SymiMeshCoordinator.gateway_identifier(config_entry):
            return False
        clean_mac = str(value)  # identifiers 用无冒号 mac_clean
    # 手动删除 mesh 设备：同步清理持久化列表与场景实体，避免重启后被重建
    runtime = _runtime(config_entry)
    coordinator = runtime.coordinator if runtime is not None else None
    if coordinator is not None and clean_mac is not None:
        for dev in coordinator.registry.all_devices():
            if dev.mac_clean == clean_mac:
                await coordinator.async_forget_device(dev.mac_address)
                break
    return True


async def async_remove_entry(hass: HomeAssistant, entry: SymiMeshConfigEntry) -> None:
    """Clean up the gateway device registry entry when the last gateway goes."""
    kind = entry.data.get("kind", KIND_GATEWAY)
    if kind != KIND_GATEWAY:
        return
    remaining = [
        e
        for e in hass.config_entries.async_entries(DOMAIN)
        if e.entry_id != entry.entry_id and e.data.get("kind") == KIND_GATEWAY
    ]
    if remaining:
        return
    from homeassistant.helpers import device_registry as dr

    from .coordinator import SymiMeshCoordinator

    identifier = SymiMeshCoordinator.gateway_identifier(entry)
    device_registry = dr.async_get(hass)
    device = device_registry.async_get_device(identifiers={(DOMAIN, identifier)})
    if device is not None:
        device_registry.async_remove_device(device.id)
        _LOGGER.info("Removed gateway device %s", identifier)
