"""Entity layer for the Symi Mesh integration.

All platforms live in this module: the factory
:func:`build_entities_for_platform` maps a discovered mesh device to the HA
entities that represent it. Unique IDs are always derived from the device MAC
(``{mac}_{key}``), satisfying HA's stable-identity requirement.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.components.cover import (
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.components.light import (
    ColorMode,
    LightEntity,
)
from homeassistant.components.lock import LockEntity
from homeassistant.components.scene import Scene
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.const import (
    PERCENTAGE,
    UnitOfTemperature,
)
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.util.color import (
    color_temperature_kelvin_to_mired,
    color_temperature_mired_to_kelvin,
)

from .const import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP,
    ATTR_RGB,
    ATTR_SWITCH,
    CLIMATE_MODE_MAP,
    CLIMATE_MODE_REVERSE,
    DEVICE_TYPE_MODELS,
    DOMAIN,
    FAN_MODE_MAP,
    FAN_MODE_REVERSE,
    MAX_MIREDS,
    MIN_MIREDS,
)
from .device import DeviceInfo as MeshDevice

_LOGGER = logging.getLogger(__name__)


def _mesh_to_mireds(color_temp_percent: int) -> int:
    """0% (warm) -> 500 mireds, 100% (cool) -> 153 mireds."""
    return round(MAX_MIREDS - (color_temp_percent / 100.0) * (MAX_MIREDS - MIN_MIREDS))


def _mireds_to_mesh(mireds: int) -> int:
    return round((MAX_MIREDS - mireds) / (MAX_MIREDS - MIN_MIREDS) * 100)


class SymiMeshEntity(Entity):
    """Base class shared by every platform entity."""

    _attr_has_entity_name = True

    def __init__(
        self,
        device: MeshDevice,
        coordinator: Any,
        entity_key: str,
        name: str,
    ) -> None:
        self._device = device
        self._coordinator = coordinator
        self._attr_unique_id = f"{device.mac_clean}_{entity_key}"
        self._attr_name = name
        model = DEVICE_TYPE_MODELS.get(device.device_type, "Symi Mesh 设备")
        if device.channels > 1:
            model = f"{model} ({device.channels}路)"
        device_info_kwargs: dict[str, Any] = {
            "identifiers": {(DOMAIN, device.mac_clean)},
            "name": device.name,
            "model": model,
            "manufacturer": "SYMI 亖米",
        }
        if coordinator.gateway_device_id is not None:
            device_info_kwargs["via_device_id"] = coordinator.gateway_device_id
        else:  # pragma: no cover - HA < 2024.10 fallback
            device_info_kwargs["via_device"] = (
                DOMAIN,
                coordinator.gateway_identifier(coordinator.entry),
            )
        self._attr_device_info = DeviceInfo(**device_info_kwargs)

    @property
    def available(self) -> bool:
        return self._device.online

    async def async_added_to_hass(self) -> None:
        # Per-device updates arrive through the coordinator listener registry.
        self._unsub = self._coordinator.add_update_listener(self._on_device_update)

    async def async_will_remove_from_hass(self) -> None:
        unsub = getattr(self, "_unsub", None)
        if unsub is not None:
            unsub()

    @callback
    def _on_device_update(self, mac: str) -> None:
        if mac == self._device.mac_address:
            self.async_write_ha_state()


# ---------------------------------------------------------------------------
# Switch (types 1, 2, 3, 9, 0x27 — one entity per channel)
# ---------------------------------------------------------------------------


class SymiSwitch(SymiMeshEntity, SwitchEntity):
    def __init__(self, device: MeshDevice, coordinator: Any, channel: int) -> None:
        self._channel_num = channel
        if device.channels == 1:
            key, name = "switch", None
        else:
            key, name = f"switch_{channel}", f"第{channel}路"
        super().__init__(device, coordinator, key, name)

    @property
    def is_on(self) -> bool | None:
        return self._device.get_channel_state(self._channel_num)

    def _bound_scene_id(self) -> int | None:
        """按键绑定的场景号（网关选项 scene_bindings："entity_id=scene_id" 列表）。"""
        entry = getattr(self._coordinator, "entry", None)
        raw = ""
        if entry is not None:
            merged = {**entry.data, **entry.options}
            raw = str(merged.get("scene_bindings") or "")
        for chunk in raw.replace("\n", ",").split(","):
            chunk = chunk.strip()
            if not chunk or "=" not in chunk:
                continue
            eid, _, sid = chunk.partition("=")
            if eid.strip() == self.entity_id:
                try:
                    return max(1, min(255, int(sid.strip())))
                except (TypeError, ValueError):
                    return None
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        # 场景绑定：turn_on = 发 0x31 场景控制（网关多设备联动），与物理按键一致
        scene_id = self._bound_scene_id()
        if scene_id is not None:
            self._device.state[f"switch_{self._channel_num}"] = True
            await self._coordinator.async_trigger_scene(scene_id)
            return
        await self._coordinator.async_set_switch(self._device, self._channel_num, True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coordinator.async_set_switch(self._device, self._channel_num, False)


class SymiCardSensor(SymiMeshEntity, BinarySensorEntity):
    """插卡取电 — card-inserted detection (device type 9)."""

    _attr_device_class = "plug"

    def __init__(self, device: MeshDevice, coordinator: Any) -> None:
        super().__init__(device, coordinator, "card_sensor", "插卡检测")

    @property
    def is_on(self) -> bool | None:
        return self._device.state.get("cardInserted")


# ---------------------------------------------------------------------------
# Lights (type 4 = CCT, 0x18 = RGB+CCT)
# ---------------------------------------------------------------------------


class SymiLight(SymiMeshEntity, LightEntity):
    def __init__(self, device: MeshDevice, coordinator: Any) -> None:
        super().__init__(device, coordinator, "light", None)
        if device.device_type == 0x18:
            self._attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}
        else:
            self._attr_supported_color_modes = {ColorMode.COLOR_TEMP}
        self._attr_color_mode = ColorMode.COLOR_TEMP

    @property
    def is_on(self) -> bool | None:
        return self._device.state.get("switch")

    @property
    def brightness(self) -> int | None:
        value = self._device.state.get("brightness")
        return None if value is None else round(value * 2.55)

    @property
    def color_temp_kelvin(self) -> int | None:
        """色温统一用开尔文暴露：HA 2026 已移除 LightEntity.color_temp。"""
        value = self._device.state.get("colorTemp")
        if value is None:
            return None
        return color_temperature_mired_to_kelvin(_mesh_to_mireds(value))

    @property
    def min_color_temp_kelvin(self) -> int:
        return color_temperature_mired_to_kelvin(MAX_MIREDS)

    @property
    def max_color_temp_kelvin(self) -> int:
        return color_temperature_mired_to_kelvin(MIN_MIREDS)

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        rgb = self._device.state.get("rgb")
        if not rgb:
            return None
        return (rgb.get("r", 0), rgb.get("g", 0), rgb.get("b", 0))

    async def async_turn_on(self, **kwargs: Any) -> None:
        state = self._device.state
        state["switch"] = True
        brightness = kwargs.get("brightness")
        if brightness is not None:
            await self._coordinator.async_send_control(
                self._device, ATTR_BRIGHTNESS, bytes([round(brightness / 2.55)])
            )
        else:
            # 恢复 mesh 上报的最后亮度 + 色温（协议无记忆字段，灯固件可能不记忆，
            # 由 HA 侧按最后上报的百分比恢复，与 mesh 显示一致）。
            last_brightness = state.get("brightness")
            last_ct = state.get("colorTemp")
            if last_brightness is not None and last_brightness > 0:
                await self._coordinator.async_send_control(
                    self._device, ATTR_BRIGHTNESS, bytes([int(last_brightness)])
                )
            if last_ct is not None:
                await self._coordinator.async_send_control(
                    self._device, ATTR_COLOR_TEMP, bytes([int(last_ct)])
                )
            if (last_brightness is None or last_brightness == 0) and last_ct is None:
                await self._coordinator.async_send_control(
                    self._device, ATTR_SWITCH, bytes([0x02])
                )
        mireds = kwargs.get("color_temp")
        if mireds is None and kwargs.get("color_temp_kelvin") is not None:
            # HA >= 2024.2 passes color_temp_kelvin.
            mireds = color_temperature_kelvin_to_mired(int(kwargs["color_temp_kelvin"]))
        if mireds is not None:
            await self._coordinator.async_send_control(
                self._device, ATTR_COLOR_TEMP, bytes([_mireds_to_mesh(int(mireds))])
            )
        if "rgb_color" in kwargs and kwargs["rgb_color"] is not None:
            r, g, b = kwargs["rgb_color"]
            await self._coordinator.async_send_control(
                self._device, ATTR_RGB, bytes([r, g, b, 0, 0])
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coordinator.async_send_control(self._device, ATTR_SWITCH, bytes([0x01]))
        self._device.state["switch"] = False


# ---------------------------------------------------------------------------
# Cover (type 5)
# ---------------------------------------------------------------------------


class SymiCover(SymiMeshEntity, CoverEntity):
    _attr_supported_features = (
        CoverEntityFeature.OPEN
        | CoverEntityFeature.CLOSE
        | CoverEntityFeature.STOP
        | CoverEntityFeature.SET_POSITION
    )

    def __init__(self, device: MeshDevice, coordinator: Any) -> None:
        super().__init__(device, coordinator, "cover", None)

    @property
    def current_cover_position(self) -> int | None:
        # 真机不上报位置，由运行方向推导：打开=100 关闭=0（用户语义）
        position = self._device.state.get("curtainPosition")
        if position is not None:
            return position
        opened = self._device.state.get("curtainOpened")
        return {True: 100, False: 0}.get(opened)

    @property
    def is_opening(self) -> bool:
        return self._device.state.get("curtainAction") == "opening"

    @property
    def is_closing(self) -> bool:
        return self._device.state.get("curtainAction") == "closing"

    @property
    def is_closed(self) -> bool | None:
        position = self._device.state.get("curtainPosition")
        if position is not None:
            return position == 0
        opened = self._device.state.get("curtainOpened")
        return {True: False, False: True}.get(opened)

    async def async_open_cover(self, **kwargs: Any) -> None:
        # 协议 CURT_RUN_STATUS: 1=打开 2=关闭 3=停止；主控权威：目标即最终状态
        self._device.state["curtainOpened"] = True
        self._device.state["curtainPosition"] = 100
        self._device.state["curtainAction"] = "stopped"
        self._device.state["curtainCmdUntil"] = time.time() + 8
        await self._coordinator.async_send_control(self._device, 0x05, bytes([1]))

    async def async_close_cover(self, **kwargs: Any) -> None:
        self._device.state["curtainOpened"] = False
        self._device.state["curtainPosition"] = 0
        self._device.state["curtainAction"] = "stopped"
        self._device.state["curtainCmdUntil"] = time.time() + 8
        await self._coordinator.async_send_control(self._device, 0x05, bytes([2]))

    async def async_stop_cover(self, **kwargs: Any) -> None:
        await self._coordinator.async_send_control(self._device, 0x05, bytes([3]))

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        position = int(kwargs["position"])
        # 主控权威：目标位置即最终状态（模组可能不回传到位/位置，直接按到位显示）
        self._device.state["curtainOpened"] = position > 0
        self._device.state["curtainPosition"] = position
        self._device.state["curtainAction"] = "stopped"
        self._device.state["curtainCmdUntil"] = time.time() + 8
        await self._coordinator.async_send_control(self._device, 0x06, bytes([position]))


# ---------------------------------------------------------------------------
# Climate (type 10 / three-in-one aircon & floor heating)
# ---------------------------------------------------------------------------


class SymiClimate(SymiMeshEntity, ClimateEntity):
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_min_temp = 16
    _attr_max_temp = 30
    _attr_target_temperature_step = 1

    def __init__(self, device: MeshDevice, coordinator: Any, variant: str = "aircon") -> None:
        # variant: "aircon" | "floor"
        self._variant = variant
        if variant == "floor":
            super().__init__(device, coordinator, "floor_heating", "地暖")
            self._attr_min_temp = 18
            self._attr_max_temp = 32
        else:
            super().__init__(device, coordinator, "climate", None)
        if variant == "aircon":
            self._attr_supported_features = (
                ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE
            )
            self._attr_hvac_modes = [
                HVACMode.OFF,
                HVACMode.COOL,
                HVACMode.HEAT,
                HVACMode.FAN_ONLY,
                HVACMode.DRY,
                HVACMode.AUTO,
            ]
            self._attr_fan_modes = ["low", "medium", "high", "auto"]
        else:
            self._attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
            self._attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]

    @property
    def current_temperature(self) -> float | None:
        return self._device.state.get("currentTemp")

    @property
    def target_temperature(self) -> float | None:
        if self._variant == "floor":
            return self._device.state.get("floorHeatingTemp")
        return self._device.state.get("targetTemp")

    @property
    def hvac_mode(self) -> HVACMode | None:
        if self._variant == "floor":
            return (
                HVACMode.HEAT
                if self._device.state.get("floorHeatingSwitch")
                else HVACMode.OFF
            )
        if not self._device.state.get("climateSwitch", self._device.state.get("switch")):
            return HVACMode.OFF
        mode = self._device.state.get("climateMode")
        ha_mode = CLIMATE_MODE_MAP.get(mode) if mode else None
        return HVACMode(ha_mode) if ha_mode else HVACMode.OFF

    @property
    def fan_mode(self) -> str | None:
        if self._variant != "aircon":
            return None
        return FAN_MODE_MAP.get(self._device.state.get("fanMode"))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        attrs: dict[str, Any] = {}
        state = self._device.state
        if "keyLock" in state:
            attrs["key_lock"] = state["keyLock"]
        if "valve" in state:
            attrs["valve"] = state["valve"]
        if "sleep" in state:
            attrs["sleep_mode"] = state["sleep"]
        if "softwareVersion" in state:
            attrs["firmware_version"] = state["softwareVersion"]
        return attrs or None

    async def async_set_temperature(self, **kwargs: Any) -> None:
        temp = int(kwargs["temperature"])
        if self._variant == "floor":
            await self._coordinator.async_send_control(
                self._device, 0x6C, bytes([temp])
            )
        else:
            await self._coordinator.async_send_control(
                self._device, 0x1B, bytes([temp])
            )

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        value = FAN_MODE_REVERSE.get(fan_mode, 4)
        await self._coordinator.async_send_control(self._device, 0x1C, bytes([value]))

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        if self._variant == "floor":
            await self._coordinator.async_send_control(
                self._device, 0x6B, bytes([0x02 if hvac_mode == HVACMode.HEAT else 0x01])
            )
            return
        if hvac_mode == HVACMode.OFF:
            await self._coordinator.async_send_control(self._device, 0x02, bytes([0x01]))
        else:
            await self._coordinator.async_send_control(self._device, 0x02, bytes([0x02]))
            mode_value = CLIMATE_MODE_REVERSE.get(hvac_mode.value)
            if mode_value:
                await self._coordinator.async_send_control(
                    self._device, 0x1D, bytes([mode_value])
                )


# ---------------------------------------------------------------------------
# Fan (three-in-one fresh air)
# ---------------------------------------------------------------------------


class SymiFan(SymiMeshEntity, FanEntity):
    _attr_supported_features = (
        FanEntityFeature.SET_SPEED
        | FanEntityFeature.PRESET_MODE
        | FanEntityFeature.DIRECTION
        | FanEntityFeature.TURN_ON
        | FanEntityFeature.TURN_OFF
    )
    # Mesh 新风只有 1/2/3 三档（4=auto 走 preset），把档位数如实告诉 HA，
    # 前端滑条才会只落 33/66/100，与 KNX 侧 max_step=3 的量化点一一对应。
    _attr_speed_count = 3

    def __init__(self, device: MeshDevice, coordinator: Any) -> None:
        super().__init__(device, coordinator, "fresh_air", "新风")
        self._attr_preset_modes = ["low", "medium", "high", "auto"]

    @property
    def is_on(self) -> bool | None:
        return self._device.state.get("freshAirSwitch")

    @property
    def percentage(self) -> int | None:
        speed = self._device.state.get("freshAirSpeed")
        if speed is None:
            return None
        # mesh: 1=high, 2=medium, 3=low, 4=auto
        return {1: 100, 2: 66, 3: 33, 4: None}.get(speed)

    @property
    def preset_mode(self) -> str | None:
        speed = self._device.state.get("freshAirSpeed")
        if speed == 4:
            return "auto"
        return FAN_MODE_MAP.get(speed)

    @property
    def current_direction(self) -> str | None:
        mode = self._device.state.get("freshAirMode")
        if mode is None:
            return None
        return "reverse" if mode == 1 else "forward"

    async def async_turn_on(
        self,
        percentage: int | None = None,
        preset_mode: str | None = None,
        **kwargs: Any,
    ) -> None:
        await self._coordinator.async_send_control(self._device, 0x68, bytes([0x02]))
        if preset_mode is not None:
            await self.async_set_preset_mode(preset_mode)
        elif percentage is not None:
            await self.async_set_percentage(percentage)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._coordinator.async_send_control(self._device, 0x68, bytes([0x01]))

    async def async_set_percentage(self, percentage: int) -> None:
        # HA percentage -> mesh speed (1=high, 2=medium, 3=low).
        # 阈值与 percentage 回读（100/66/33）互逆，保证 33/66/100 三个点自洽；
        # 0% 等同于关机，不能落成低风（HA 前端把滑条拉到 0 就是关）。
        if percentage <= 0:
            await self.async_turn_off()
            return
        speed = 1 if percentage >= 84 else (2 if percentage >= 50 else 3)
        await self._coordinator.async_send_control(self._device, 0x6A, bytes([speed]))

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        if preset_mode == "auto":
            await self._coordinator.async_send_control(self._device, 0x6A, bytes([4]))
        else:
            value = FAN_MODE_REVERSE.get(preset_mode, 3)
            await self._coordinator.async_send_control(self._device, 0x6A, bytes([value]))

    async def async_set_direction(self, direction: str) -> None:
        await self._coordinator.async_send_control(
            self._device, 0x69, bytes([1 if direction == "reverse" else 0])
        )


# ---------------------------------------------------------------------------
# Sensors
# ---------------------------------------------------------------------------


class SymiTemperatureSensor(SymiMeshEntity, SensorEntity):
    _attr_device_class = "temperature"
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = "measurement"

    def __init__(self, device: MeshDevice, coordinator: Any) -> None:
        super().__init__(device, coordinator, "temperature", "温度")

    @property
    def native_value(self) -> float | None:
        return self._device.state.get("temperature")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        battery = self._device.state.get("battery")
        if battery is None:
            return None
        return {"battery_level": battery}


class SymiHumiditySensor(SymiMeshEntity, SensorEntity):
    _attr_device_class = "humidity"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = "measurement"

    def __init__(self, device: MeshDevice, coordinator: Any) -> None:
        super().__init__(device, coordinator, "humidity", "湿度")

    @property
    def native_value(self) -> int | None:
        return self._device.state.get("humidity")

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        battery = self._device.state.get("battery")
        if battery is None:
            return None
        return {"battery_level": battery}


class SymiDoorSensor(SymiMeshEntity, BinarySensorEntity):
    _attr_device_class = "door"

    def __init__(self, device: MeshDevice, coordinator: Any) -> None:
        super().__init__(device, coordinator, "door", None)

    @property
    def is_on(self) -> bool | None:
        return self._device.state.get("door")


class SymiMotionSensor(SymiMeshEntity, BinarySensorEntity):
    _attr_device_class = "motion"

    def __init__(self, device: MeshDevice, coordinator: Any) -> None:
        super().__init__(device, coordinator, "motion", None)

    @property
    def is_on(self) -> bool | None:
        return self._device.state.get("motion")


class SymiWaterSensor(SymiMeshEntity, BinarySensorEntity):
    _attr_device_class = "moisture"

    def __init__(self, device: MeshDevice, coordinator: Any) -> None:
        super().__init__(device, coordinator, "water_alarm", "水浸报警")

    @property
    def is_on(self) -> bool | None:
        return self._device.state.get("waterAlarm")


class SymiSmokeSensor(SymiMeshEntity, BinarySensorEntity):
    _attr_device_class = "smoke"

    def __init__(self, device: MeshDevice, coordinator: Any) -> None:
        super().__init__(device, coordinator, "smoke_alarm", "烟雾报警")

    @property
    def is_on(self) -> bool | None:
        return self._device.state.get("smokeAlarm")


# ---------------------------------------------------------------------------
# Lock (type 14)
# ---------------------------------------------------------------------------


class SymiLock(SymiMeshEntity, LockEntity):
    def __init__(self, device: MeshDevice, coordinator: Any) -> None:
        super().__init__(device, coordinator, "lock", None)

    @property
    def is_locked(self) -> bool | None:
        # The mesh protocol exposes on/off only; map to locked/unlocked.
        return self._device.state.get("switch")

    async def async_lock(self, **kwargs: Any) -> None:
        await self._coordinator.async_send_control(self._device, 0x02, bytes([0x02]))

    async def async_unlock(self, **kwargs: Any) -> None:
        await self._coordinator.async_send_control(self._device, 0x02, bytes([0x01]))


# ---------------------------------------------------------------------------
# Diagnostic sensors (voice controller / TV box — no standard control domain)
# ---------------------------------------------------------------------------


class SymiDiagnosticSensor(SymiMeshEntity, SensorEntity):
    _attr_entity_category = "diagnostic"

    def __init__(self, device: MeshDevice, coordinator: Any, key: str, name: str) -> None:
        super().__init__(device, coordinator, key, name)

    @property
    def native_value(self) -> str | None:
        return self._device.state.get("softwareVersion") or "unknown"


# ---------------------------------------------------------------------------
# Scene (types 6, 12, 0x14)
# ---------------------------------------------------------------------------


class SymiScene(SymiMeshEntity, Scene):
    def __init__(self, device: MeshDevice, coordinator: Any, scene_id: int = 1) -> None:
        super().__init__(device, coordinator, "scene", None)
        self._scene_id = scene_id

    async def async_activate(self, **kwargs: Any) -> None:
        await self._coordinator.async_trigger_scene(self._scene_id)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_entities_for_platform(platform: str, device: MeshDevice, coordinator: Any) -> list:
    """Return the entities of ``platform`` that represent ``device``."""
    entities: list = []
    device_type = device.device_type

    if platform == "switch":
        if device_type in (1, 2, 3, 0x27):
            for channel in range(1, device.channels + 1):
                entities.append(SymiSwitch(device, coordinator, channel))
        elif device_type == 9:
            entities.append(SymiSwitch(device, coordinator, 1))

    elif platform == "binary_sensor":
        if device_type == 7:
            entities.append(SymiDoorSensor(device, coordinator))
        elif device_type == 8:
            entities.append(SymiMotionSensor(device, coordinator))
        elif device_type == 9:
            entities.append(SymiCardSensor(device, coordinator))
        elif device_type == 15:
            entities.append(SymiWaterSensor(device, coordinator))
        elif device_type == 16:
            entities.append(SymiSmokeSensor(device, coordinator))

    elif platform == "lock":
        if device_type == 14:
            entities.append(SymiLock(device, coordinator))

    elif platform == "sensor":
        if device_type == 11:
            entities.append(SymiTemperatureSensor(device, coordinator))
            entities.append(SymiHumiditySensor(device, coordinator))
        elif device_type == 13:
            entities.append(
                SymiDiagnosticSensor(device, coordinator, "voice_status", "语控状态")
            )
        elif device_type == 17:
            entities.append(
                SymiDiagnosticSensor(device, coordinator, "tvbox_status", "机顶盒状态")
            )

    elif platform == "light":
        if device_type in (4, 0x18):
            entities.append(SymiLight(device, coordinator))

    elif platform == "cover":
        if device_type == 5:
            entities.append(SymiCover(device, coordinator))

    elif platform == "climate":
        if device_type == 10:
            entities.append(SymiClimate(device, coordinator, "aircon"))
            # 地暖实体只在地暖属性真正上报后才创建（详见 device.py 取证逻辑）。
            if device.has_floor_heating:
                entities.append(SymiClimate(device, coordinator, "floor"))

    elif platform == "fan":
        if device.has_fresh_air:
            entities.append(SymiFan(device, coordinator))

    elif platform == "scene":
        if device_type in (6, 12, 0x14):
            entities.append(SymiScene(device, coordinator))

    return entities
