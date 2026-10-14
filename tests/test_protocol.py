"""Protocol unit tests (pure Python, no Home Assistant required).

Run: python -m pytest tests/ -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "custom_components"))

from symi_mesh.const import (  # noqa: E402
    OP_DEVICE_CONTROL,
    OP_RESP_DEVICE_LIST,
    PROTOCOL_HEADER,
)
from symi_mesh.device import DeviceRegistry  # noqa: E402
from symi_mesh.protocol import (  # noqa: E402
    ProtocolHandler,
    build_switch_state,
    calculate_checksum,
    parse_device_list_frame,
    parse_status_event,
    parse_switch_state,
)


def _checksum(data: bytes) -> int:
    checksum = 0
    for byte in data:
        checksum ^= byte
    return checksum & 0xFF


def _event_frame(opcode: int, status: int, payload: bytes) -> bytes:
    """Build an event/response-format frame: 53 op status len payload cksum."""
    head = bytes([0x53, opcode, status, len(payload)])
    return head + payload + bytes([_checksum(head + payload)])


class TestFrameCodec:
    def setup_method(self) -> None:
        self.handler = ProtocolHandler()

    def test_build_frame_roundtrip(self) -> None:
        frame = self.handler.build_frame(OP_DEVICE_CONTROL, bytes([0x01, 0x02, 0x03]))
        assert frame[0] == PROTOCOL_HEADER
        assert frame[1] == OP_DEVICE_CONTROL
        assert frame[2] == 3
        assert frame[3:6] == bytes([0x01, 0x02, 0x03])
        assert frame[6] == _checksum(frame[:6])
        parsed = self.handler.parse_frame(frame)
        assert parsed is not None
        assert parsed.payload == bytes([0x01, 0x02, 0x03])

    def test_checksum_rejects_corruption(self) -> None:
        frame = bytearray(self.handler.build_frame(OP_DEVICE_CONTROL, bytes([0x01])))
        frame[-1] ^= 0xFF
        assert self.handler.parse_frame(bytes(frame)) is None

    def test_read_device_list_frame(self) -> None:
        frame = self.handler.build_read_device_list_frame()
        assert frame == bytes([0x53, 0x12, 0x00, 0x41])

    def test_add_data_resyncs_on_garbage(self) -> None:
        good = self.handler.build_read_device_list_frame()
        noisy = bytes([0x00, 0xFF, 0x42]) + good
        frames = self.handler.add_data(noisy)
        assert len(frames) == 1
        assert frames[0].opcode == 0x12

    def test_add_data_splits_chunks(self) -> None:
        frame = self.handler.build_read_device_list_frame()
        assert self.handler.add_data(frame[:2]) == []
        frames = self.handler.add_data(frame[2:])
        assert len(frames) == 1

    def test_oversized_frame_dropped(self) -> None:
        # Claim a huge length; the parser must resync, not allocate.
        bogus = bytes([0x53, 0x30, 0xFF])
        assert self.handler.add_data(bogus) == []


class TestSwitchState:
    def test_single_channel(self) -> None:
        assert build_switch_state(1, 1, True) == 0x02
        assert build_switch_state(1, 1, False) == 0x01

    def test_two_channel_defaults(self) -> None:
        # 2ch all-off default 0x05; channel 1 on -> 0x06 (10 01)
        assert build_switch_state(2, 1, True) == 0x06
        assert build_switch_state(2, 1, True, 0x06) == 0x06
        assert build_switch_state(2, 2, True, 0x05) == 0x09  # 10 01 -> 10 10

    def test_four_channel(self) -> None:
        assert build_switch_state(4, 1, True) == 0x56  # 10 01 01 01
        assert build_switch_state(4, 4, True, 0x55) == 0x95  # 10 01 01 01

    def test_eight_channel_little_endian(self) -> None:
        value = build_switch_state(8, 1, True)
        assert isinstance(value, bytes) and value == bytes([0x56, 0x55])
        assert parse_switch_state(value, 8) == [True, False, False, False, False, False, False, False]

    def test_parse_roundtrip(self) -> None:
        packed = build_switch_state(4, 2, True)
        states = parse_switch_state(packed, 4)
        assert states == [False, True, False, False]


class TestEvents:
    def setup_method(self) -> None:
        self.handler = ProtocolHandler()

    def test_parse_status_event(self) -> None:
        # 53 80 06 04 9A 01 02 15 <cksum>: addr=0x019A, attr=0x02, param=0x15
        payload = bytes([0x9A, 0x01, 0x02, 0x15])
        frame = _event_frame(0x80, 0x06, payload)
        parsed = self.handler.parse_frame(frame)
        assert parsed is not None
        event = parse_status_event(parsed)
        assert event["networkAddress"] == 0x019A
        assert event["attrType"] == 0x02
        assert event["parameters"] == bytes([0x15])
        assert event["subOpcode"] == 0x06

    def test_device_list_frame_parse(self) -> None:
        payload = bytes(
            [
                2,  # maxDevices
                0,  # index
                0xCC, 0xDA, 0x20, 0xB8, 0x4A, 0x89,  # MAC
                0x10, 0x01,  # network address 0x0110
                0xA8, 0x01,  # vendor 0x01A8
                0x02,  # device type: single-fire switch
                0x04,  # sub type: 4 channels
                0x01,  # status: online
                0x00,  # reserved trailing byte (gateway sends >= 16 bytes)
            ]
        )
        frame = _event_frame(OP_RESP_DEVICE_LIST, 0x00, payload)
        parsed = self.handler.parse_frame(frame)
        assert parsed is not None
        info = parse_device_list_frame(parsed)
        assert info is not None
        assert info["maxDevices"] == 2
        assert info["macAddress"] == "cc:da:20:b8:4a:89"
        assert info["networkAddress"] == 0x0110
        assert info["deviceType"] == 2
        assert info["deviceSubType"] == 4
        assert info["online"] is True

    def test_device_list_frame_too_short(self) -> None:
        frame = _event_frame(OP_RESP_DEVICE_LIST, 0x00, bytes([1, 0]))
        parsed = self.handler.parse_frame(frame)
        assert parsed is not None
        assert parse_device_list_frame(parsed) is None


class TestDeviceModel:
    def test_registry_mapping(self) -> None:
        registry = DeviceRegistry()
        device, is_new = registry.add_or_update(
            {
                "macAddress": "AA:BB:CC:DD:EE:FF",
                "networkAddress": 0x1234,
                "deviceType": 1,
                "deviceSubType": 3,
            }
        )
        assert is_new
        assert device.channels == 3
        assert device.mac_clean == "aabbccddeeff"
        assert registry.get_by_address(0x1234) is device
        assert registry.get_by_mac("AA:BB:CC:DD:EE:FF") is device

    def test_switch_state_update(self) -> None:
        registry = DeviceRegistry()
        device, _ = registry.add_or_update(
            {
                "macAddress": "AA:BB:CC:DD:EE:FF",
                "networkAddress": 0x0100,
                "deviceType": 1,
                "deviceSubType": 2,
            }
        )
        # 0x06 = 10 01 -> channel 1 on, channel 2 off
        device.update_from_event(0x02, bytes([0x06]))
        assert device.state["switch_1"] is True
        assert device.state["switch_2"] is False
        assert device.get_channel_state(1) is True
        assert device.get_current_switch_state() == 0x06

    def test_thermostat_temp(self) -> None:
        registry = DeviceRegistry()
        device, _ = registry.add_or_update(
            {
                "macAddress": "AA:BB:CC:DD:EE:FF",
                "networkAddress": 0x0200,
                "deviceType": 10,
                "deviceSubType": 1,
            }
        )
        # 0x16: 23.45 C -> 2345 (0x0929) little-endian
        device.update_from_event(0x16, bytes([0x29, 0x09]))
        assert device.state["currentTemp"] == 23.45
        device.update_from_event(0x1B, bytes([25]))
        assert device.state["targetTemp"] == 25
        device.update_from_event(0x1D, bytes([1]))
        assert device.state["climateMode"] == 1

    def test_three_in_one_promotion(self) -> None:
        registry = DeviceRegistry()
        device, _ = registry.add_or_update(
            {
                "macAddress": "AA:BB:CC:DD:EE:FF",
                "networkAddress": 0x0300,
                "deviceType": 10,
                "deviceSubType": 1,
            }
        )
        # Answering a fresh-air switch query marks the panel as three-in-one.
        device.update_from_event(0x68, bytes([0x02]))
        assert device.is_three_in_one is True
        assert device.state["freshAirSwitch"] is True
