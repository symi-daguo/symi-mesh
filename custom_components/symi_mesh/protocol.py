"""Symi Mesh wire protocol codec.

Ported from node-red-contrib-symi-mesh/lib/protocol.js. Pure Python, no Home
Assistant imports, so it can be unit-tested standalone.

Frame layout
------------
Commands:  ``53 [opcode] [len] [payload...] [checksum]``
Events/responses: ``53 [opcode] [status] [len] [payload...] [checksum]``

The checksum is the XOR of every preceding byte. Incoming data is fed through
:meth:`ProtocolHandler.add_data`, which re-syncs on the 0x53 header and
returns complete frames.
"""
from __future__ import annotations

import logging
from typing import Final

from .const import (
    EVENT_LIST_NODE_UPDATE_RPT,
    MAX_FRAME_LENGTH,
    MIN_FRAME_LENGTH,
    MSG_TYPE_PARAM_LEN,
    OP_EVENT_NODE_STATUS,
    OP_EVENT_TRANSPARENT_MSG,
    OP_RESP_DEVICE_LIST,
    OP_SCENE_CONTROL,
    OP_SCENE_CONTROL_TRANSPARENT,
    PROTOCOL_HEADER,
    SCENE_BROADCAST_ADDR,
)

_LOGGER = logging.getLogger(__name__)

_BUFFER_TIMEOUT_MS: Final = 10_000


def calculate_checksum(data: bytes) -> int:
    """XOR checksum over ``data``."""
    checksum = 0
    for byte in data:
        checksum ^= byte
    return checksum & 0xFF


class ProtocolFrame:
    """A single decoded frame."""

    __slots__ = ("header", "opcode", "length", "payload", "checksum", "status")

    def __init__(
        self,
        header: int,
        opcode: int,
        length: int,
        payload: bytes,
        checksum: int,
        status: int | None = None,
    ) -> None:
        self.header = header
        self.opcode = opcode
        self.length = length
        self.payload = payload
        self.checksum = checksum
        self.status = status

    def is_response(self) -> bool:
        return self.opcode >= 0x81

    def is_event(self) -> bool:
        return self.opcode in (OP_EVENT_NODE_STATUS, OP_EVENT_TRANSPARENT_MSG)

    def is_device_status_event(self) -> bool:
        return self.opcode == OP_EVENT_NODE_STATUS

    def is_transparent_event(self) -> bool:
        return self.opcode == OP_EVENT_TRANSPARENT_MSG

    def __repr__(self) -> str:  # pragma: no cover - debug helper
        return (
            f"ProtocolFrame(opcode=0x{self.opcode:02X}, "
            f"status={self.status if self.status is None else hex(self.status)}, "
            f"len={self.length}, payload={self.payload.hex()})"
        )


def build_switch_state(
    channels: int, target_channel: int, target_state: bool, current_state: int | None = None
) -> int | bytes:
    """Encode the packed 2-bits-per-channel switch state value."""
    if not 1 <= channels <= 8:
        raise ValueError(f"Invalid channels count: {channels}")
    if not 1 <= target_channel <= channels:
        raise ValueError(f"Invalid target channel: {target_channel}")

    if channels == 1:
        return 0x02 if target_state else 0x01

    if channels <= 4:
        defaults = {2: 0x05, 3: 0x15, 4: 0x55}
        state_value = defaults[channels] if current_state is None else current_state
        bit_pos = (target_channel - 1) * 2
        state_value = (state_value & ~(0x03 << bit_pos)) | (
            (0x02 if target_state else 0x01) << bit_pos
        )
        return state_value & 0xFF

    # 6/8 channels: 16-bit value, little-endian on the wire.
    state_value = 0x5555 if current_state is None else current_state
    if state_value < 0x100:
        state_value |= 0x5500
    bit_pos = (target_channel - 1) * 2
    state_value = (state_value & ~(0x03 << bit_pos)) | (
        (0x02 if target_state else 0x01) << bit_pos
    )
    return bytes([state_value & 0xFF, (state_value >> 8) & 0xFF])


def parse_switch_state(state_value: int | bytes, channels: int) -> list[bool]:
    """Decode packed switch state into per-channel booleans."""
    if isinstance(state_value, (bytes, bytearray)):
        if len(state_value) < 2:
            raise ValueError("Switch state buffer too short")
        value = state_value[0] | (state_value[1] << 8)
    else:
        value = state_value

    states = []
    for i in range(channels):
        bits = (value >> (i * 2)) & 0x03
        states.append(bits == 0x02)
    return states


class ProtocolHandler:
    """Frame builder/parser with a re-syncing input buffer."""

    def __init__(self) -> None:
        self.buffer = bytearray()
        self._last_data_time = 0.0
        self._buffer_timeout_ms = _BUFFER_TIMEOUT_MS

    # -- frame construction -------------------------------------------------

    def build_frame(self, opcode: int, data: bytes = b"") -> bytes:
        frame = bytearray([PROTOCOL_HEADER, opcode, len(data)])
        frame.extend(data)
        frame.append(calculate_checksum(bytes(frame)))
        return bytes(frame)

    def build_read_software_version_frame(self) -> bytes:
        return self.build_frame(0x02)

    def build_read_device_list_frame(self) -> bytes:
        return self.build_frame(0x12)

    def build_device_status_query_frame(self, network_addr: int, msg_type: int = 0x00) -> bytes:
        addr = network_addr & 0xFFFF
        if msg_type == 0x00:
            payload = bytes([addr & 0xFF, (addr >> 8) & 0xFF])
        else:
            payload = bytes([addr & 0xFF, (addr >> 8) & 0xFF, msg_type & 0xFF])
        return self.build_frame(0x32, payload)

    def build_enable_status_report_frame(self, network_addr: int) -> bytes:
        addr = network_addr & 0xFFFF
        return self.build_frame(0x30, bytes([addr & 0xFF, (addr >> 8) & 0xFF, 0x10, 0x01]))

    def build_switch_control_frame(
        self,
        network_addr: int,
        channels: int,
        target_channel: int,
        target_state: bool,
        current_state: int | None = None,
    ) -> bytes:
        state_value = build_switch_state(channels, target_channel, target_state, current_state)
        attr_type = 0x45 if isinstance(state_value, bytes) else 0x02
        addr = network_addr & 0xFFFF
        head = bytes([addr & 0xFF, (addr >> 8) & 0xFF, attr_type])
        if isinstance(state_value, bytes):
            payload = head + state_value
        else:
            payload = head + bytes([state_value])
        return self.build_frame(0x30, payload)

    def build_device_control_frame(self, network_addr: int, attr_type: int, param: bytes = b"") -> bytes:
        """Generic control frame with clamping per attribute type."""
        addr = network_addr & 0xFFFF
        head = bytes([addr & 0xFF, (addr >> 8) & 0xFF, attr_type & 0xFF])

        if attr_type in (0x03, 0x04):  # brightness / colour temp 0-100
            value = max(0, min(100, param[0])) if param else 0
            payload = head + bytes([value])
        elif attr_type == 0x05:  # curtain action 1-3
            value = max(1, min(3, param[0])) if param else 1
            payload = head + bytes([value])
        elif attr_type == 0x06:  # curtain position 0-100
            value = max(0, min(100, param[0])) if param else 0
            payload = head + bytes([value])
        elif attr_type == 0x4C:  # RGB: R G B WW CW
            rgb = list(param[:5]) + [0] * (5 - len(param[:5]))
            payload = head + bytes(max(0, min(255, v)) for v in rgb)
        elif attr_type == 0x1B:  # target temperature 16-30
            value = max(16, min(30, param[0])) if param else 20
            payload = head + bytes([value])
        elif attr_type == 0x1C:  # fan speed 1-4
            value = max(1, min(4, param[0])) if param else 1
            payload = head + bytes([value])
        elif attr_type == 0x1D:  # climate mode 1-4
            value = max(1, min(4, param[0])) if param else 1
            payload = head + bytes([value])
        else:
            payload = head + param
        return self.build_frame(0x30, payload)

    def build_scene_control_frame(self, scene_id: int) -> bytes:
        """Standard scene control (0x31, group broadcast)."""
        return self.build_frame(OP_SCENE_CONTROL, bytes([scene_id & 0xFF]))

    def build_scene_control_frame_transparent(self, scene_id: int) -> bytes:
        """Scene execution for transparent slave nodes (0x34, broadcast + 5 retries)."""
        addr = SCENE_BROADCAST_ADDR
        payload = bytes(
            [
                addr & 0xFF,
                (addr >> 8) & 0xFF,
                0x00,  # ACK: none
                0x05,  # retransmit 5 times (1.2 s)
                0x01,  # scene execute
                scene_id & 0xFF,
            ]
        )
        return self.build_frame(OP_SCENE_CONTROL_TRANSPARENT, payload)

    # -- frame parsing ------------------------------------------------------

    def parse_frame(self, data: bytes) -> ProtocolFrame | None:
        if len(data) < MIN_FRAME_LENGTH or data[0] != PROTOCOL_HEADER:
            return None

        opcode = data[1]
        if opcode >= 0x80:
            # Event / response format: 53 opcode status len payload checksum
            if len(data) < 5:
                return None
            status = data[2]
            param_length = data[3]
            if len(data) != 5 + param_length:
                return None
            payload = data[4 : 4 + param_length]
            checksum = data[4 + param_length]
        else:
            # Command format: 53 opcode len payload checksum
            param_length = data[2]
            if len(data) != 4 + param_length:
                return None
            payload = data[3 : 3 + param_length]
            checksum = data[3 + param_length]
            status = None

        if checksum != calculate_checksum(data[:-1]):
            return None
        return ProtocolFrame(PROTOCOL_HEADER, opcode, param_length, payload, checksum, status)

    def add_data(self, data: bytes) -> list[ProtocolFrame]:
        """Feed raw bytes, return the complete frames extracted."""
        import time

        now = time.monotonic() * 1000.0
        if self.buffer and now - self._last_data_time > self._buffer_timeout_ms:
            self.buffer.clear()
        self._last_data_time = now
        self.buffer.extend(data)

        frames: list[ProtocolFrame] = []
        while len(self.buffer) >= MIN_FRAME_LENGTH:
            header_index = self.buffer.find(PROTOCOL_HEADER)
            if header_index == -1:
                # Keep the last two bytes in case a header straddles chunks.
                if len(self.buffer) > 2:
                    del self.buffer[:-2]
                else:
                    self.buffer.clear()
                break
            if header_index > 0:
                del self.buffer[:header_index]

            if len(self.buffer) < 3:
                break
            opcode = self.buffer[1]
            if opcode >= 0x80:
                if len(self.buffer) < 4:
                    break
                total_length = 5 + self.buffer[3]
            else:
                total_length = 4 + self.buffer[2]

            if total_length > MAX_FRAME_LENGTH:
                del self.buffer[:1]
                continue
            if len(self.buffer) < total_length:
                break

            frame = self.parse_frame(bytes(self.buffer[:total_length]))
            if frame is not None:
                frames.append(frame)
            del self.buffer[:total_length]

        return frames

    def clear_buffer(self) -> None:
        self.buffer.clear()


def parse_status_event(frame: ProtocolFrame) -> dict:
    """Parse a 0x80 node-status event (first msg_type/param pair).

    Payload: ``[addr_lo] [addr_hi] [attr_type] [params...]`` (little-endian).
    """
    if not frame.is_device_status_event():
        raise ValueError("Not a status event frame")
    if len(frame.payload) < 3:
        raise ValueError("Status event payload too short")

    network_address = frame.payload[0] | (frame.payload[1] << 8)
    attr_type = frame.payload[2]
    parameters = frame.payload[3:]
    return {
        "networkAddress": network_address,
        "attrType": attr_type,
        "parameters": parameters,
        "subOpcode": frame.status,
    }


def parse_status_pairs(payload: bytes) -> list[tuple[int, bytes]]:
    """Split a NODE_STATUS payload into ``(msg_type, param)`` pairs.

    A single event may report several attributes back to back (protocol doc
    §4.4 NODE_STATUS example). Unknown/variable-length msg_types stop the
    split — the remaining bytes are returned as one trailing pair only when
    the msg_type is known but truncated.
    """
    pairs: list[tuple[int, bytes]] = []
    index = 0
    while index < len(payload):
        msg_type = payload[index]
        param_len = MSG_TYPE_PARAM_LEN.get(msg_type)
        if param_len is None:
            # Unknown attribute: keep the remainder as its params so callers
            # can still inspect it, then stop.
            pairs.append((msg_type, payload[index + 1 :]))
            break
        params = payload[index + 1 : index + 1 + param_len]
        pairs.append((msg_type, params))
        index += 1 + param_len
    return pairs


def parse_transparent_event(frame: ProtocolFrame) -> dict:
    """Parse a 0xC0 transparent event (big-endian address)."""
    if not frame.is_transparent_event():
        raise ValueError("Not a transparent event frame")
    if len(frame.payload) < 2:
        raise ValueError("Transparent event payload too short")
    return {
        "networkAddress": (frame.payload[0] << 8) | frame.payload[1],
        "transparentData": frame.payload[2:],
    }


def parse_device_list_frame(frame: ProtocolFrame) -> dict | None:
    """Parse a device-list frame (0x92 response or 0x80/0x10 update event).

    Payload: ``[max_devices] [index] [mac:6] [net_addr:2 LE] [vendor:2 LE]
    [device_type] [sub_type] [status]`` (status bit0 = online).
    """
    is_list_response = frame.opcode == OP_RESP_DEVICE_LIST
    is_list_update = frame.opcode == OP_EVENT_NODE_STATUS and frame.status == EVENT_LIST_NODE_UPDATE_RPT
    if not (is_list_response or is_list_update):
        return None
    payload = frame.payload
    if len(payload) < 16:
        _LOGGER.debug("Device list frame too short: %d bytes", len(payload))
        return None

    return {
        "maxDevices": payload[0],
        "index": payload[1],
        "macAddress": ":".join(f"{b:02x}" for b in payload[2:8]),
        "networkAddress": payload[8] | (payload[9] << 8),
        "vendorId": payload[10] | (payload[11] << 8),
        "deviceType": payload[12],
        "deviceSubType": payload[13],
        "online": (payload[14] & 0x01) == 0x01,
    }
