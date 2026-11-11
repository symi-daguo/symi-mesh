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
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import (
    CONF_NAME,
    CONF_SCENE_ID,
    CONF_SCENE_MODE,
    DOMAIN,
    KIND_SCENE,
    SCENE_MODE_TRANSPARENT,
)
from .coordinator import SymiMeshCoordinator
from .entity import build_device_info
from .runtime import SymiMeshConfigEntry

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
        self._attr_device_info = build_device_info(coordinator, info)

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


class SymiManualScene(Scene):
    """用户手动录入的场景：只认场景号，不需要物理面板先触发一次。

    协议里场景表存在网关内部（0x20 配置、0x31 按场景号执行），所以只要知道
    场景号就能下发。默认走 0x31 标准群控；公版/离线节点可切 0x34 透传。
    """

    _attr_should_poll = False

    def __init__(
        self,
        hass: HomeAssistant,
        entry: SymiMeshConfigEntry,
        scene_id: int,
        name: str,
        mode: str,
    ) -> None:
        self._hass = hass
        self._entry_id = entry.entry_id
        self._scene_id = scene_id
        self._mode = mode
        self._attr_unique_id = f"symi_mesh_scene_manual_{scene_id}"
        self._attr_name = name
        self._attr_state = "待触发"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"scene_id": self._scene_id, "mode": self._mode}

    async def async_activate(self, **kwargs: Any) -> None:
        sent = 0
        for entry in self._hass.config_entries.async_entries(DOMAIN):
            runtime = getattr(entry, "runtime_data", None)
            coordinator = getattr(runtime, "coordinator", None) if runtime else None
            if coordinator is None:
                continue
            try:
                if self._mode == SCENE_MODE_TRANSPARENT:
                    await coordinator.async_trigger_scene_transparent(self._scene_id)
                else:
                    await coordinator.async_trigger_scene(self._scene_id)
                sent += 1
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("场景 %s 下发失败: %s", self._scene_id, err)

        self._attr_state = datetime.now().strftime("%H:%M:%S")
        self.async_write_ha_state()
        if sent == 0:
            _LOGGER.warning("场景 %s 已触发，但没有在线的网关可下发", self._scene_id)
        self._hass.bus.async_fire(
            f"{DOMAIN}_scene_activated",
            {"entity_id": self.entity_id, "scene_id": self._scene_id, "sent": sent},
        )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: SymiMeshConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up scene entities from the gateway coordinator."""
    if entry.data.get("kind") == KIND_SCENE:
        async_add_entities(
            [
                SymiManualScene(
                    hass,
                    entry,
                    int(entry.data[CONF_SCENE_ID]),
                    str(entry.data.get(CONF_NAME) or entry.title),
                    str(entry.data.get(CONF_SCENE_MODE) or "group"),
                )
            ]
        )
        return

    coordinator = entry.runtime_data.coordinator
    if coordinator is None:  # pragma: no cover - entry without a gateway link
        return
    coordinator.register_platform("scene", async_add_entities)
