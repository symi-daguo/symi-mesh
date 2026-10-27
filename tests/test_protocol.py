"""协议层单测：帧编解码、校验、分片重组、状态事件解析（不依赖 Home Assistant）。"""
from __future__ import annotations

import pytest

from symi_mesh.protocol import (
    ProtocolHandler,
    calculate_checksum,
    parse_device_list_frame,
    parse_status_event,
    parse_status_pairs,
)


@pytest.fixture
def handler() -> ProtocolHandler:
    return ProtocolHandler()


def test_checksum_is_xor() -> None:
    assert calculate_checksum(b"\x53\x12\x00") == 0x53 ^ 0x12 ^ 0x00


def test_command_frame_roundtrip(handler: ProtocolHandler) -> None:
    frame = handler.build_read_device_list_frame()
    assert frame[0] == 0x53
    assert frame[1] == 0x12
    parsed = handler.add_data(frame)
    assert len(parsed) == 1
    assert parsed[0].opcode == 0x12
    assert parsed[0].payload == b""


def test_bad_checksum_is_rejected(handler: ProtocolHandler) -> None:
    frame = bytearray(handler.build_read_device_list_frame())
    frame[-1] ^= 0xFF
    assert handler.add_data(bytes(frame)) == []


def test_serial_fragmentation_is_reassembled(handler: ProtocolHandler) -> None:
    frame = handler.build_device_status_query_frame(0x0100, 0x68)
    parsed = []
    for byte in frame:
        parsed.extend(handler.add_data(bytes([byte])))
    assert len(parsed) == 1
    assert parsed[0].payload == b"\x00\x01\x68"


def test_multiple_frames_in_one_chunk(handler: ProtocolHandler) -> None:
    chunk = handler.build_read_device_list_frame() + handler.build_read_software_version_frame()
    assert len(handler.add_data(chunk)) == 2


def test_noise_before_header_is_dropped(handler: ProtocolHandler) -> None:
    frame = handler.build_read_device_list_frame()
    assert len(handler.add_data(b"\x00\xff\xaa" + frame)) == 1


def test_status_event_parsing() -> None:
    payload = bytes([0x00, 0x01, 0x94]) + bytes([0x00, 0x00, 0x88, 24, 26, 0x80, 22, 0x80])
    body = bytes([0x53, 0x80, 0x00, len(payload)]) + payload
    frame_bytes = body + bytes([calculate_checksum(body)])

    frame = ProtocolHandler().add_data(frame_bytes)[0]
    event = parse_status_event(frame)
    assert event["networkAddress"] == 0x0100
    assert event["attrType"] == 0x94
    assert len(event["parameters"]) == 8


def test_status_pairs_split_known_lengths() -> None:
    # 0x02 定长 1、0x1B 定长 1、0x94 未登记 -> 剩余归它并停止
    payload = bytes([0x02, 0x02, 0x1B, 24, 0x94]) + bytes(range(8))
    pairs = parse_status_pairs(payload)
    assert [p[0] for p in pairs] == [0x02, 0x1B, 0x94]
    assert pairs[1][1] == bytes([24])
    assert len(pairs[2][1]) == 8


def test_device_list_frame_requires_16_bytes() -> None:
    short = bytes([0x53, 0x92, 0x00, 15]) + bytes(15)
    frame = ProtocolHandler().add_data(short + bytes([calculate_checksum(short)]))[0]
    assert parse_device_list_frame(frame) is None


def test_device_list_frame_fields() -> None:
    payload = (
        bytes([1, 0])
        + bytes.fromhex("aabbcc001122")
        + bytes([0x30, 0x12])
        + bytes([0xA8, 0x01])
        + bytes([10, 1, 1, 0])
    )
    body = bytes([0x53, 0x92, 0x00, len(payload)]) + payload
    frame = ProtocolHandler().add_data(body + bytes([calculate_checksum(body)]))[0]
    parsed = parse_device_list_frame(frame)
    assert parsed is not None
    assert parsed["macAddress"] == "aa:bb:cc:00:11:22"
    assert parsed["networkAddress"] == 0x1230
    assert parsed["deviceType"] == 10
    assert parsed["online"] is True
