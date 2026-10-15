"""scene platform for the Symi Mesh integration.

Every mesh "network scene" (reported via the 0x11 NET_SCENE_ACT event, i.e. a
scene key press) becomes a scene entity. The entity is a *receiver*: it does not
send anything to the gateway — it only surfaces the scene activation so HA
automations can bind it to a KNX scene (via ``knx.send``) or any other action.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from homeassistant.components.scene import Scene
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import SymiMeshCoordinator

_LOGGER = logging.getLogger(__name__)


class SymiSceneEntity(Scene):
    """A mesh network scene surfaced as a receiver scene entity."""

    _attr_should_poll = False

    def __init__(
        self,
        coordinator: SymiMeshCoordinator,
        mac_address: str,
        device_name: str,
        scene_id: int,
    ) -> None:
        from homeassistant.helpers.device_registry import DeviceInfo

        self._coordinator = coordinator
        self._mac = mac_address
        self._scene_id = scene_id
        self._attr_unique_id = f"symi_mesh_scene_{mac_address}_{scene_id}"
        self._attr_name = f"{device_name} 场景{scene_id}"
        self._attr_state = "待触发"
        info: dict[str, Any] = {
            "identifiers": {(DOMAIN, mac_address)},
            "name": device_name,
            "manufacturer": "SYMI 亖米",
        }
        if coordinator.gateway_device_id is not None:
            info["via_device_id"] = coordinator.gateway_device_id
        else:  # pragma: no cover - HA < 2024.10
            info["via_device"] = (DOMAIN, coordinator.gateway_identifier(coordinator.entry))
        self._attr_device_info = DeviceInfo(**info)

    async def async_activate(self, **kwargs: Any) -> None:
        """Activate the mesh network scene via 0x34 transparent broadcast.

        与物理按键触发一致：0x34 广播到 mesh 网络（重传 5 次），网络内设备
        执行该场景。执行完成后设备状态上报（0x11 场景通知）再回填实体。
        """
        await self._coordinator.async_trigger_scene_transparent(self._scene_id)
        self._mark_activated()

    @callback
    def _mark_activated(self) -> None:
        self._attr_state = datetime.now().strftime("%H:%M:%S")
        self.async_write_ha_state()
        self._coordinator.hass.bus.async_fire(
            f"{DOMAIN}_scene_activated",
            {
                "entity_id": self.entity_id,
                "mac": self._mac,
                "scene_id": self._scene_id,
            },
        )

    @callback
    def notify_scene(self) -> None:
        """Called by the coordinator when the mesh scene is reported (0x11)."""
        self._mark_activated()


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up scene entities from the gateway coordinator."""
    coordinator: SymiMeshCoordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    coordinator.register_platform("scene", async_add_entities)
