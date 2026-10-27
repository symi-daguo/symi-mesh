"""设备模型单测：三合一取证、0x94 组合状态、无效值过滤、通道数。"""
from __future__ import annotations

import pytest

from custom_components.symi_mesh.device import DeviceInfo

BASE = {
    "macAddress": "AA:BB:CC:00:11:22",
    "networkAddress": 0x0100,
    "deviceType": 10,
    "deviceSubType": 1,
}


def make(**overrides) -> DeviceInfo:
    return DeviceInfo({**BASE, **overrides})


def test_mac_clean_and_name() -> None:
    device = make()
    assert device.mac_clean == "aabbcc001122"
    assert device.name.startswith("温控器_")


def test_fresh_air_only_does_not_create_floor() -> None:
    device = make()
    device.update_from_event(0x6A, bytes([2]))
    assert device.has_fresh_air is True
    assert device.has_floor_heating is False
    assert device.is_three_in_one is True


def test_floor_only_does_not_create_fresh_air() -> None:
    device = make()
    device.update_from_event(0x6C, bytes([26]))
    assert device.has_floor_heating is True
    assert device.has_fresh_air is False


def test_non_type10_ignores_subsystem_markers() -> None:
    device = make(deviceType=2, deviceSubType=1)
    device.update_from_event(0x6A, bytes([2]))
    assert device.is_three_in_one is False
    assert device.has_fresh_air is False


def test_three_in_one_combined_state() -> None:
    device = make()
    # byte2 = 0x8A: bit7 开机、bits3-5=001(heat)、bits0-2=010(medium)
    payload = bytes([0x00, 0x00, 0x8A, 24, 26, 0x80, 22, 0xB2])
    device.update_from_event(0x94, payload)

    assert device.has_fresh_air and device.has_floor_heating
    assert device.state["climateSwitch"] is True
    assert device.state["climateMode"] == 2      # 模式位 001 -> heat
    assert device.state["fanMode"] == 2          # 档位 2 -> medium
    assert device.state["targetTemp"] == 24
    assert device.state["currentTemp"] == 26
    assert device.state["floorHeatingSwitch"] is True
    assert device.state["floorHeatingTemp"] == 22
    assert device.state["freshAirSwitch"] is True
    assert device.state["freshAirSpeed"] == 2


def test_floor_temperature_accepts_16_and_rejects_zero() -> None:
    device = make()
    device.update_from_event(0x94, bytes([0x00, 0x00, 0x80, 24, 26, 0x80, 16, 0x80]))
    assert device.state["floorHeatingTemp"] == 16

    other = make()
    other.update_from_event(0x94, bytes([0x00, 0x00, 0x80, 24, 26, 0x80, 0, 0x80]))
    assert "floorHeatingTemp" not in other.state


def test_room_temperature_zero_is_ignored() -> None:
    device = make()
    device.update_from_event(0x16, bytes([0x00, 0x00]))
    assert "currentTemp" not in device.state

    device.update_from_event(0x16, bytes([0x10, 0x0E]))  # 3600 -> 36.00
    assert device.state["currentTemp"] == 36.0


def test_target_temperature_outside_range_is_ignored() -> None:
    device = make()
    device.update_from_event(0x1B, bytes([15]))
    assert "targetTemp" not in device.state
    device.update_from_event(0x1B, bytes([22]))
    assert device.state["targetTemp"] == 22


def test_channel_count_from_sub_type() -> None:
    assert make(deviceType=2, deviceSubType=4).channels == 4
    assert make(deviceType=2, deviceSubType=9).channels == 8   # 上限截断
    assert make(deviceType=10, deviceSubType=1).channels == 1


def test_persistence_keeps_three_in_one_evidence() -> None:
    device = make()
    device.update_from_event(0x94, bytes([0x00, 0x00, 0x80, 24, 26, 0x80, 22, 0x80]))
    dumped = device.to_dict()
    assert dumped["isThreeInOne"] is True
    assert dumped["hasFreshAir"] is True
    assert dumped["hasFloorHeating"] is True

    restored = DeviceInfo({**BASE, **dumped})
    assert restored.has_fresh_air is True
    assert restored.has_floor_heating is True
