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
    parse_status_pairs,
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


class TestMultiPairStatus:
    """NODE_STATUS payloads may carry several (msg_type, param) pairs."""

    def setup_method(self) -> None:
        self.handler = ProtocolHandler()

    def test_real_capture_thermostat_frame(self) -> None:
        # Real gateway capture: 53 80 05 0D 06 01 02 02 16 00 00 1B 10 1C 00 1D 00 C0
        payload = bytes([0x06, 0x01, 0x02, 0x02, 0x16, 0x00, 0x00, 0x1B, 0x10, 0x1C, 0x00, 0x1D, 0x00])
        frame = _event_frame(0x80, 0x05, payload)
        parsed = self.handler.parse_frame(frame)
        assert parsed is not None
        pairs = parse_status_pairs(parsed.payload[2:])
        assert [p[0] for p in pairs] == [0x02, 0x16, 0x1B, 0x1C, 0x1D]
        assert pairs[0][1] == bytes([0x02])
        assert pairs[1][1] == bytes([0x00, 0x00])
        assert pairs[2][1] == bytes([0x10])
        assert pairs[3][1] == bytes([0x00])
        assert pairs[4][1] == bytes([0x00])

    def test_device_applies_all_pairs(self) -> None:
        registry = DeviceRegistry()
        device, _ = registry.add_or_update(
            {
                "macAddress": "CD:D2:57:93:46:84",
                "networkAddress": 0x0106,
                "deviceType": 10,
                "deviceSubType": 1,
            }
        )
        pairs = parse_status_pairs(
            bytes([0x02, 0x02, 0x16, 0x00, 0x00, 0x1B, 0x10, 0x1C, 0x00, 0x1D, 0x00])
        )
        device.update_from_pairs(pairs)
        assert device.state["switch"] is True       # 空调开
        assert device.state["currentTemp"] == 0.0   # 室温 0.00 C
        assert device.state["targetTemp"] == 16     # 设定 16 C
        assert device.state["fanMode"] == 0
        assert device.state["climateMode"] == 0

    def test_signed_temperature(self) -> None:
        registry = DeviceRegistry()
        device, _ = registry.add_or_update(
            {
                "macAddress": "AA:BB:CC:DD:EE:FF",
                "networkAddress": 0x0300,
                "deviceType": 11,
                "deviceSubType": 1,
            }
        )
        # -5.00 C -> -500 = 0xFE0C (signed LE)
        device.update_from_event(0x16, bytes([0x0C, 0xFE]))
        assert device.state["temperature"] == -5.0

    def test_scene_panel_event_keys(self) -> None:
        registry = DeviceRegistry()
        device, _ = registry.add_or_update(
            {
                "macAddress": "AA:BB:CC:DD:EE:FF",
                "networkAddress": 0x0400,
                "deviceType": 6,
                "deviceSubType": 6,
            }
        )
        # Doc: 0x40 0x00 = key 1 triggered; 0x00 0x10 = key 6 triggered.
        device.update_from_event(0x07, bytes([0x40, 0x00]))
        assert device.state["sceneTriggers"][0] is True
        assert not any(device.state["sceneTriggers"][1:])
        device.update_from_event(0x07, bytes([0x00, 0x10]))
        assert device.state["sceneTriggers"][5] is True
        assert device.state["sceneTriggers"][0] is False

    def test_unknown_msg_type_stops_split(self) -> None:
        pairs = parse_status_pairs(bytes([0x99, 0x01, 0x02]))
        assert len(pairs) == 1
        assert pairs[0][0] == 0x99
        assert pairs[0][1] == bytes([0x01, 0x02])


class TestListUpdateEvent:
    def setup_method(self) -> None:
        self.handler = ProtocolHandler()

    def test_node_list_update_event_parses(self) -> None:
        # Doc §4.2: 53 80 10 10 ... — node list update, same payload as 0x92.
        payload = bytes(
            [
                0x0A, 0x09,
                0xB5, 0x91, 0xB5, 0x7A, 0xFA, 0x28,
                0x21, 0x01, 0xA8, 0x01,
                0x0A, 0x00, 0x01, 0x00,
            ]
        )
        frame = _event_frame(0x80, 0x10, payload)
        parsed = self.handler.parse_frame(frame)
        assert parsed is not None
        info = parse_device_list_frame(parsed)
        assert info is not None
        assert info["maxDevices"] == 10
        assert info["index"] == 9
        assert info["macAddress"] == "b5:91:b5:7a:fa:28"
        assert info["networkAddress"] == 0x0121

    def test_standard_scene_frame(self) -> None:
        # Doc §4.3: 53 31 01 02 61
        frame = self.handler.build_scene_control_frame(2)
        assert frame == bytes([0x53, 0x31, 0x01, 0x02, 0x61])

    def test_transparent_scene_frame(self) -> None:
        # Doc §6: 53 34 06 bc c0 00 05 01 02 1B
        frame = self.handler.build_scene_control_frame_transparent(2)
        assert frame == bytes([0x53, 0x34, 0x06, 0xBC, 0xC0, 0x00, 0x05, 0x01, 0x02, 0x1B])

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


class TestOptimisticSwitch:
    """Rapid multi-channel toggles must not read stale packed state."""

    def _device(self) -> object:
        registry = DeviceRegistry()
        device, _ = registry.add_or_update(
            {
                "macAddress": "AA:BB:CC:DD:EE:FF",
                "networkAddress": 0x0100,
                "deviceType": 1,
                "deviceSubType": 4,
            }
        )
        return device

    def test_optimistic_update_composes_packed_state(self) -> None:
        device = self._device()
        # 1-2-3-4 rapid toggle: each optimistic write keeps prior channels on.
        device.set_channel_state(1, True)
        assert device.get_current_switch_state() == 0x56
        device.set_channel_state(2, True)
        assert device.get_current_switch_state() == 0x5A
        device.set_channel_state(3, True)
        assert device.get_current_switch_state() == 0x6A
        device.set_channel_state(4, True)
        assert device.get_current_switch_state() == 0xAA

    def test_stale_report_dropped_within_window(self) -> None:
        device = self._device()
        device.set_channel_state(3, True)  # optimistic: channel 3 on
        assert device.state["switch_3"] is True
        # A stale 0x80 report (0x5A: ch1+ch2 on, ch3 off) arrives while the
        # optimistic window is still open -> must NOT bounce channel 3 off.
        device.update_from_event(0x02, bytes([0x5A]))
        assert device.state["switch_3"] is True

    def test_matching_report_confirms_and_closes_window(self) -> None:
        device = self._device()
        device.set_channel_state(3, True, window_s=0.2)
        # Matching report (0x6A: ch1+ch2+ch3 on) confirms optimistic state.
        device.update_from_event(0x02, bytes([0x6A]))
        assert device.state["switch_3"] is True

    def test_report_applies_after_window_expires(self) -> None:
        device = self._device()
        device.set_channel_state(3, True, window_s=-1.0)  # window already expired
        # A genuine physical report (0x55 all off) must now apply.
        device.update_from_event(0x02, bytes([0x55]))
        assert device.state["switch_3"] is False


class TestLoopBreaker:
    """死循环熔断：窗口内写入超限熔断单实体，其余实体与集成不受影响。"""

    def test_trips_after_limit_and_recovers(self) -> None:
        import asyncio

        from symi_mesh.knx_sync import SymiSyncEngine

        async def run() -> None:
            class FakeBus:
                def __init__(self) -> None:
                    self.fired = []

                def async_fire(self, event, data):
                    self.fired.append((event, data))

            class FakeHass:
                data = {}

            hass = FakeHass()
            hass.bus = FakeBus()
            eng = SymiSyncEngine(
                hass,
                {"name": "t", "entities": ["switch.a", "switch.b"],
                 "direction": "two_way", "enabled": True},
            )
            for _ in range(10):
                eng._record_write("switch.a")
            assert "switch.a" in eng._loop_disabled
            assert "switch.b" not in eng._loop_disabled
            assert hass.bus.fired[0][0] == "symi_mesh_loop_breaker"
            # 9 次不触发
            hass2 = FakeHass()
            hass2.bus = FakeBus()
            eng2 = SymiSyncEngine(
                hass2,
                {"name": "t2", "entities": ["switch.c"], "direction": "two_way",
                 "enabled": True},
            )
            for _ in range(9):
                eng2._record_write("switch.c")
            assert "switch.c" not in eng2._loop_disabled
            # 冷却过期后 _enqueue 自动恢复
            eng._loop_disabled["switch.a"] = 0.0
            try:
                eng._enqueue("switch.a", {"state": "on"}, "switch", "switch", 1)
            except AttributeError:
                pass  # FakeHass 无 states；恢复逻辑已在异常前执行
            assert "switch.a" not in eng._loop_disabled

        asyncio.run(run())
