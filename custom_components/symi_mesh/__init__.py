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
    SERVICE_SCAN_DEVICES,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    kind = entry.data.get("kind", KIND_GATEWAY)
    hass.data.setdefault(DOMAIN, {})
    if kind == KIND_KNX_SYNC:
        return await _async_setup_sync_entry(hass, entry)
    return await _async_setup_gateway_entry(hass, entry)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    kind = entry.data.get("kind", KIND_GATEWAY)
    if kind == KIND_KNX_SYNC:
        return await _async_unload_sync_entry(hass, entry)
    return await _async_unload_gateway_entry(hass, entry)


# ---------------------------------------------------------------------------
# Gateway entries
# ---------------------------------------------------------------------------


async def _async_setup_gateway_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .coordinator import SymiMeshCoordinator

    coordinator = SymiMeshCoordinator(hass, entry)
    hass.data[DOMAIN][entry.entry_id] = {"coordinator": coordinator}
    await coordinator.async_setup()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _async_register_services(hass)
    return True


async def _async_unload_gateway_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if data and (coordinator := data.get("coordinator")):
        await coordinator.async_shutdown()
    _async_maybe_remove_services(hass)
    return unload_ok


# ---------------------------------------------------------------------------
# KNX sync entries
# ---------------------------------------------------------------------------


async def _async_setup_sync_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    from .const import (
        CONF_SYNC_DIRECTION,
        CONF_SYNC_ENABLED,
        CONF_SYNC_ENTITIES,
        CONF_SYNC_NAME,
        SYNC_DIRECTION_TWO_WAY,
    )
    from .knx_sync import SymiSyncEngine
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
    hass.data[DOMAIN][entry.entry_id] = {"engine": engine}
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    _LOGGER.info("已启用同步组「%s」: %s", group[CONF_SYNC_NAME], group[CONF_SYNC_ENTITIES])
    return True


async def _async_unload_sync_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if data and (engine := data.get("engine")):
        await engine.async_stop()
    _async_maybe_remove_services(hass)
    return True


# ---------------------------------------------------------------------------
# Shared
# ---------------------------------------------------------------------------


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
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


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.data[DOMAIN].get("_services"):
        return

    async def _reload_all(_call: Any) -> None:
        for eid in list(hass.data.get(DOMAIN, {})):
            if str(eid).startswith("_"):
                continue
            await hass.config_entries.async_reload(eid)

    async def _scan_devices(call: Any) -> None:
        from .coordinator import SymiMeshCoordinator

        count = 0
        for _eid, data in list(hass.data.get(DOMAIN, {}).items()):
            if str(_eid).startswith("_") or not isinstance(data, dict):
                continue
            coordinator: SymiMeshCoordinator | None = data.get("coordinator")
            if coordinator is not None:
                count = await coordinator.async_scan_devices()
        _LOGGER.info("设备扫描完成，当前设备数: %s", count)

    async def _describe(_call: Any) -> None:
        snaps = []
        for eid, data in hass.data.get(DOMAIN, {}).items():
            if str(eid).startswith("_") or not isinstance(data, dict):
                continue
            engine = data.get("engine")
            snaps.append(
                {
                    "entry_id": eid,
                    "snapshot": engine.describe() if engine is not None else None,
                }
            )
        _LOGGER.info("symi_mesh 状态: %s", snaps)
        hass.bus.async_fire(f"{DOMAIN}_describe", {"entries": snaps})

    hass.services.async_register(DOMAIN, SERVICE_RELOAD, _reload_all)
    hass.services.async_register(DOMAIN, SERVICE_SCAN_DEVICES, _scan_devices)
    hass.services.async_register(DOMAIN, SERVICE_DESCRIBE, _describe)
    hass.data[DOMAIN]["_services"] = True


def _async_maybe_remove_services(hass: HomeAssistant) -> None:
    remaining = [k for k in hass.data.get(DOMAIN, {}) if not str(k).startswith("_")]
    if remaining:
        return
    if not hass.data[DOMAIN].pop("_services", None):
        return
    for service in (SERVICE_RELOAD, SERVICE_SCAN_DEVICES, SERVICE_DESCRIBE):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)


async def async_remove_config_entry_device(
    hass: HomeAssistant, config_entry: ConfigEntry, device_entry
) -> bool:
    """允许用户从设备页面删除无效设备（含其全部实体）。

    网关本体设备不可删（删除即卸载集成，走条目删除）；mesh 子设备一律可删——
    删除后若网关仍在设备列表中上报该设备，会作为新设备重新被发现。
    """
    from .coordinator import gateway_identifier

    if config_entry.data.get("kind", KIND_GATEWAY) != KIND_GATEWAY:
        return True
    identifiers = device_entry.identifiers or set()
    for domain, value in identifiers:
        if domain == DOMAIN and value == gateway_identifier(config_entry):
            return False
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
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
