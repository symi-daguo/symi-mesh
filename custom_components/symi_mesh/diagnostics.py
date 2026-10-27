"""Diagnostics support for the Symi Mesh integration."""
from __future__ import annotations

from typing import Any

from homeassistant.core import HomeAssistant

from .const import KIND_GATEWAY
from .runtime import SymiMeshConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: SymiMeshConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry (no secrets are stored)."""
    data = {**entry.data, **entry.options}
    redacted = {
        "kind": data.get("kind"),
        "connection_type": data.get("connection_type"),
        "host": data.get("host"),
        "port": data.get("port"),
        "serial_port": data.get("serial_port"),
        "baud_rate": data.get("baud_rate"),
    }

    if data.get("kind") != KIND_GATEWAY:
        return {"entry": redacted}

    runtime = getattr(entry, "runtime_data", None)
    coordinator = runtime.coordinator if runtime is not None else None
    if coordinator is None:
        return {"entry": redacted}

    devices = []
    for device in coordinator.registry.all_devices():
        devices.append(
            {
                "mac": device.mac_address,
                "name": device.name,
                "type": device.device_type,
                "subType": device.device_sub_type,
                "channels": device.channels,
                "networkAddress": f"0x{device.network_address:04X}",
                "online": device.online,
                "isThreeInOne": device.is_three_in_one,
                "state": device.state,
            }
        )

    transport = coordinator.transport
    return {
        "entry": redacted,
        "connected": bool(transport and transport.connected),
        "deviceCount": len(devices),
        "devices": devices,
    }
