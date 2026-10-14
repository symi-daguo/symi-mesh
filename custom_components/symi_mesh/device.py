"""Device model for the Symi Mesh integration.

Ported from node-red-contrib-symi-mesh/lib/device-manager.js (DeviceInfo +
DeviceManager). Pure Python, no Home Assistant imports.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable

from .const import (
    ATTR_BRIGHTNESS,
    ATTR_CARD_DET,
    ATTR_COLOR_TEMP,
    ATTR_CURTAIN_POSITION,
    ATTR_CURTAIN_RUN_STATUS,
    ATTR_DOOR,
    ATTR_FLOOR_HEATING_SWITCH,
    ATTR_FLOOR_HEATING_TEMP,
    ATTR_FRESH_AIR_MODE,
    ATTR_FRESH_AIR_SPEED,
    ATTR_FRESH_AIR_SWITCH,
    ATTR_MOTION,
    ATTR_RGB,
    ATTR_SENSOR_HUMI,
    ATTR_SENSOR_TEMP,
    ATTR_CLIMATE_FAN,
    ATTR_CLIMATE_MODE,
    ATTR_CLIMATE_TEMP,
    ATTR_SWITCH,
    ATTR_SWITCH_6_8,
    ATTR_THREE_IN_ONE,
    DEVICE_TYPE_NAMES,
    DEVICE_TYPE_TO_PLATFORM,
    MULTI_CHANNEL_TYPES,
)
from .protocol import parse_switch_state

_LOGGER = logging.getLogger(__name__)

_CHANNEL_DEFAULTS = {2: 0x05, 3: 0x15, 4: 0x55, 6: 0x5555, 8: 0x5555}


class DeviceInfo:
    """A single mesh device discovered on the gateway."""

    def __init__(self, data: dict[str, Any], registry: "DeviceRegistry | None" = None) -> None:
        self.mac_address: str = data["macAddress"]
        self.network_address: int = data["networkAddress"]
        self.device_type: int = data["deviceType"]
        self.device_sub_type: int = data.get("deviceSubType") or 1
        self.vendor_id: int = data.get("vendorId") or 0x01A8
        self.online: bool = data.get("online", True)
        self.state: dict[str, Any] = {}
        self.last_seen: float = time.time()
        self.is_three_in_one: bool = False
        self.registry = registry
        self.name = data.get("name") or self.generate_name()
        self.channels = self.get_channel_count()

    # -- identity -----------------------------------------------------------

    @property
    def mac_clean(self) -> str:
        """Lower-case MAC without separators — used for unique IDs and topics."""
        return self.mac_address.replace(":", "").lower()

    def generate_name(self) -> str:
        type_name = DEVICE_TYPE_NAMES.get(self.device_type, "Device")
        return f"{type_name}_{self.mac_clean[-12:]}"

    def get_channel_count(self) -> int:
        if self.device_type in MULTI_CHANNEL_TYPES:
            return max(1, min(8, self.device_sub_type))
        return 1

    def get_entity_platform(self) -> str:
        if self.is_three_in_one:
            return "three_in_one"
        return DEVICE_TYPE_TO_PLATFORM.get(self.device_type, "switch")

    # -- state --------------------------------------------------------------

    def update_last_seen(self) -> None:
        self.last_seen = time.time()

    def update_from_event(self, attr_type: int, parameters: bytes) -> None:
        """Apply a 0x80 event payload to the local state (ported verbatim)."""
        self.update_last_seen()

        if attr_type in (0x01, 0x02, 0x03, 0x04):
            # Thermostat fan quirk: attrType itself is the fan speed, no params.
            if self.device_type == 10 and len(parameters) == 0:
                self.state["fanMode"] = attr_type
                return
            if attr_type == ATTR_BRIGHTNESS and parameters:
                self.state["brightness"] = parameters[0]
                self.state["switch"] = parameters[0] > 0
            elif attr_type == ATTR_COLOR_TEMP and parameters:
                self.state["colorTemp"] = parameters[0]
            elif attr_type == ATTR_SWITCH and parameters and self.device_type != 5:
                self.handle_switch_state(parameters, attr_type)

        elif attr_type == ATTR_SWITCH_6_8:
            # 6-8ch switch report (also sent by panels regardless of channels).
            if self.device_type != 5:
                self.handle_switch_state(parameters, attr_type)

        elif attr_type == ATTR_CURTAIN_RUN_STATUS:
            if parameters:
                status = parameters[0]
                self.state["curtainStatus"] = status
                if status == 0:
                    self.state["curtainProtocol"] = "mijia"
                    self.state["curtainAction"] = "opening"
                elif status == 3:
                    self.state["curtainProtocol"] = "miniprogram"
                    self.state["curtainAction"] = "stopped"
                elif status == 1:
                    if self.state.get("curtainProtocol") == "mijia":
                        self.state["curtainAction"] = "closing"
                    else:
                        self.state["curtainAction"] = "opening"
                elif status == 2:
                    if self.state.get("curtainProtocol") == "mijia":
                        self.state["curtainAction"] = "stopped"
                    else:
                        self.state["curtainAction"] = "closing"

        elif attr_type == ATTR_CURTAIN_POSITION:
            if parameters:
                self.state["curtainPosition"] = parameters[0]

        elif attr_type == ATTR_DOOR:
            if parameters:
                self.state["door"] = parameters[0] == 0x01

        elif attr_type == ATTR_MOTION:
            if parameters:
                self.state["motion"] = parameters[0] == 0x01

        elif attr_type == ATTR_CARD_DET:
            if parameters:
                card = parameters[0]
                self.state["cardInserted"] = card in (0x01, 0x1)
                self.state["switch"] = self.state["cardInserted"]

        elif attr_type == ATTR_RGB:
            if len(parameters) >= 5:
                self.state["rgb"] = {
                    "r": parameters[0],
                    "g": parameters[1],
                    "b": parameters[2],
                    "ww": parameters[3],
                    "cw": parameters[4],
                }

        elif attr_type == ATTR_SENSOR_TEMP:
            if len(parameters) >= 2:
                temp = (parameters[0] | (parameters[1] << 8)) / 100.0
                if self.device_type == 10:
                    self.state["currentTemp"] = temp
                elif self.device_type == 11:
                    self.state["temperature"] = temp
                else:
                    self.state["currentTemp"] = temp
                    self.state["temperature"] = temp

        elif attr_type == ATTR_SENSOR_HUMI:
            if parameters:
                self.state["humidity"] = parameters[0]

        elif attr_type == ATTR_CLIMATE_TEMP:
            if parameters and 16 <= parameters[0] <= 30:
                self.state["targetTemp"] = parameters[0]

        elif attr_type == ATTR_CLIMATE_FAN:
            if parameters:
                self.state["fanMode"] = parameters[0]

        elif attr_type == ATTR_CLIMATE_MODE:
            if parameters:
                self.state["climateMode"] = parameters[0]

        elif attr_type == ATTR_FRESH_AIR_SWITCH:
            if parameters:
                if self.device_type == 10:
                    self.is_three_in_one = True
                self.state["freshAirSwitch"] = parameters[0] == 0x02
            elif self.is_three_in_one:
                self.state["freshAirSwitch"] = False

        elif attr_type == ATTR_FRESH_AIR_MODE:
            if self.is_three_in_one and parameters:
                self.state["freshAirMode"] = parameters[0]

        elif attr_type == ATTR_FRESH_AIR_SPEED:
            if parameters:
                if self.device_type == 10:
                    self.is_three_in_one = True
                self.state["freshAirSpeed"] = parameters[0]
            elif self.is_three_in_one:
                self.state["freshAirSpeed"] = 4

        elif attr_type == ATTR_FLOOR_HEATING_SWITCH:
            if parameters:
                if self.device_type == 10:
                    self.is_three_in_one = True
                self.state["floorHeatingSwitch"] = parameters[0] == 0x02
            elif self.is_three_in_one:
                self.state["floorHeatingSwitch"] = False

        elif attr_type == ATTR_FLOOR_HEATING_TEMP:
            if self.is_three_in_one and parameters:
                self.state["floorHeatingTemp"] = parameters[0]
                self.state["floorHeatingSwitch"] = True

        elif attr_type == ATTR_THREE_IN_ONE:
            if len(parameters) >= 8:
                if self.device_type == 10:
                    self.is_three_in_one = True
                self._handle_three_in_one_state(parameters)

    def _handle_three_in_one_state(self, parameters: bytes) -> None:
        """0x94 combined status (8 bytes, per R&D struct definition)."""
        byte2, byte3, byte4 = parameters[2], parameters[3], parameters[4]
        byte5, byte6, byte7 = parameters[5], parameters[6], parameters[7]

        # Air conditioner: byte2 bit7=on, bit3-5=mode, bit0-2=fan level
        self.state["climateSwitch"] = (byte2 & 0x80) != 0
        ctrl_mode = (byte2 >> 3) & 0x07
        mode_map = {0: 1, 1: 2, 2: 3, 3: 4, 4: 1}
        if ctrl_mode in mode_map:
            self.state["climateMode"] = mode_map[ctrl_mode]
        fan_level = byte2 & 0x07
        fan_map = {0: 4, 1: 3, 2: 2, 4: 1}
        self.state["fanMode"] = fan_map.get(fan_level, 3)
        if 16 <= byte3 <= 30:
            self.state["targetTemp"] = byte3
        if 10 <= byte4 <= 50:
            self.state["currentTemp"] = byte4

        # Floor heating: byte5 bit7=on, byte6=temperature
        self.state["floorHeatingSwitch"] = (byte5 & 0x80) != 0
        if 18 <= byte6 <= 32:
            self.state["floorHeatingTemp"] = byte6

        # Fresh air: byte7 bit7=on, bit4=mode, bit0-2=fan level
        self.state["freshAirSwitch"] = (byte7 & 0x80) != 0
        self.state["freshAirMode"] = (byte7 >> 4) & 0x01
        fresh_fan = byte7 & 0x07
        self.state["freshAirSpeed"] = fan_map.get(fresh_fan, 4)

    def handle_switch_state(self, parameters: bytes, attr_type: int) -> list[dict]:
        """Decode packed switch state; returns the list of changed channels."""
        if not parameters:
            return []

        # Determine control source (0x0D marker: 2 = mesh/network control).
        state_bytes = 2 if (self.channels > 4 or attr_type == ATTR_SWITCH_6_8) else 1
        if len(parameters) >= state_bytes + 2 and parameters[state_bytes] == 0x0D:
            self.state["lastSource"] = parameters[state_bytes + 1]

        if self.channels == 1:
            value = parameters[0]
            if self.device_type == 10:
                self.state["switch"] = value == 0x02
            else:
                self.state["switch"] = value == 0x02
            return []

        if len(parameters) >= 2:
            value = parameters[0] | (parameters[1] << 8)
        else:
            value = parameters[0]
        self.state["switchState"] = value

        changed = []
        for i in range(self.channels):
            bit_value = (value >> (i * 2)) & 0x03
            if bit_value in (0x01, 0x02):
                new_state = bit_value == 0x02
                key = f"switch_{i + 1}"
                old_state = self.state.get(key)
                if old_state is not None and old_state != new_state:
                    changed.append({"button": i + 1, "oldState": old_state, "newState": new_state})
                self.state[key] = new_state
        return changed

    def get_current_switch_state(self) -> int | None:
        """Re-pack per-channel state for control commands."""
        if self.channels == 1:
            return 0x02 if self.state.get("switch") else 0x01
        if self.channels <= 8:
            state_value = 0
            for i in range(self.channels):
                bit_value = 0x02 if self.state.get(f"switch_{i + 1}") else 0x01
                state_value |= bit_value << (i * 2)
            return state_value
        return None

    def get_channel_state(self, channel: int) -> bool:
        if self.channels == 1:
            return bool(self.state.get("switch"))
        return bool(self.state.get(f"switch_{channel}"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "macAddress": self.mac_address,
            "networkAddress": self.network_address,
            "deviceType": self.device_type,
            "deviceSubType": self.device_sub_type,
            "vendorId": self.vendor_id,
            "name": self.name,
            "channels": self.channels,
            "online": self.online,
            "isThreeInOne": self.is_three_in_one,
        }


class DeviceRegistry:
    """MAC <-> network address registry with change listeners."""

    def __init__(self) -> None:
        self.devices: dict[str, DeviceInfo] = {}
        self.mac_to_address: dict[str, int] = {}
        self.address_to_mac: dict[int, str] = {}
        self._listeners: list[Callable[[str, DeviceInfo, bool], None]] = []

    def add_listener(self, callback: Callable[[str, DeviceInfo, bool], None]) -> None:
        """callback(mac, device, is_new)."""
        self._listeners.append(callback)

    def add_or_update(self, data: dict[str, Any]) -> tuple[DeviceInfo, bool]:
        mac = data["macAddress"]
        addr = data["networkAddress"]
        device = self.devices.get(mac)
        is_new = device is None

        if is_new:
            device = DeviceInfo(data, self)
            self.devices[mac] = device
        else:
            if device.network_address != addr:
                _LOGGER.info(
                    "Device %s address changed: 0x%04X -> 0x%04X",
                    mac,
                    device.network_address,
                    addr,
                )
                device.network_address = addr
            device.online = data.get("online", True)
            device.update_last_seen()

        self.mac_to_address[mac] = addr
        self.address_to_mac[addr] = mac

        for listener in list(self._listeners):
            try:
                listener(mac, device, is_new)
            except Exception:  # pragma: no cover - listener isolation
                _LOGGER.exception("Device registry listener failed")
        return device, is_new

    def get_by_mac(self, mac: str) -> DeviceInfo | None:
        return self.devices.get(mac)

    def get_by_address(self, addr: int) -> DeviceInfo | None:
        mac = self.address_to_mac.get(addr)
        return self.devices.get(mac) if mac else None

    def all_devices(self) -> list[DeviceInfo]:
        return list(self.devices.values())

    def mark_three_in_one(self, mac: str) -> None:
        device = self.devices.get(mac)
        if device and not device.is_three_in_one:
            device.is_three_in_one = True
