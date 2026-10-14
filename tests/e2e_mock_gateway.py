"""End-to-end integration test inside the HA container.

Starts a mock Symi gateway on 127.0.0.1:4196, then drives the real
integration coordinator against it: device discovery, status-event push,
control-command write. Verifies the whole stack minus HA's entity registry.
"""
from __future__ import annotations

import asyncio
import sys
import types

sys.path.insert(0, "/config")

from custom_components.symi_mesh.const import OP_DEVICE_CONTROL, OP_EVENT_NODE_STATUS, OP_RESP_DEVICE_LIST  # noqa: E402
from custom_components.symi_mesh.coordinator import SymiMeshCoordinator  # noqa: E402
from custom_components.symi_mesh.entity import build_entities_for_platform  # noqa: E402
from custom_components.symi_mesh.protocol import ProtocolHandler, calculate_checksum  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(f"{name} {detail}".strip())
    print(("PASS  " if condition else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))


def event_frame(opcode: int, status: int, payload: bytes) -> bytes:
    head = bytes([0x53, opcode, status, len(payload)])
    return head + payload + bytes([calculate_checksum(head + payload)])


def device_list_frame(index: int, mac: bytes, addr: int, dtype: int, subtype: int, online: bool) -> bytes:
    payload = bytes(
        [2, index, *mac, addr & 0xFF, (addr >> 8) & 0xFF, 0xA8, 0x01, dtype, subtype, 0x01 if online else 0x00, 0x00]
    )
    return event_frame(OP_RESP_DEVICE_LIST, 0x00, payload)


class MockGateway:
    """Speaks just enough of the Symi protocol for the coordinator."""

    def __init__(self) -> None:
        self.server: asyncio.AbstractServer | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.received: list[bytes] = []
        self.handler = ProtocolHandler()

    async def start(self) -> None:
        self.server = await asyncio.start_server(self._on_client, "127.0.0.1", 4196)

    async def stop(self) -> None:
        if self.writer is not None:
            self.writer.close()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _on_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        # Two devices: 2-channel switch + thermostat.
        await self._send(device_list_frame(0, bytes([0xCC, 0xDA, 0x20, 0xB8, 0x4A, 0x89]), 0x0110, 1, 2, True))
        await self._send(device_list_frame(1, bytes([0x11, 0x22, 0x33, 0x44, 0x55, 0x66]), 0x0200, 10, 1, True))
        while True:
            try:
                data = await reader.read(4096)
            except Exception:
                return
            if not data:
                return
            self.received.append(data)
            for frame in self.handler.add_data(data):
                await self._respond(frame)

    async def _respond(self, frame) -> None:
        opcode = frame.opcode
        if opcode == 0x10:  # enable status report
            await self._send(event_frame(0xB0, 0x00, frame.payload))
        elif opcode == 0x32:  # status query -> B2 marker + 0x80 data
            addr = frame.payload[0] | (frame.payload[1] << 8)
            await self._send(event_frame(0xB2, 0x00, frame.payload))
            if addr == 0x0110:
                await self._send(event_frame(OP_EVENT_NODE_STATUS, 0x05, bytes([0x10, 0x01, 0x02, 0x06])))
            else:
                await self._send(event_frame(OP_EVENT_NODE_STATUS, 0x06, bytes([0x00, 0x02, 0x16, 0x91, 0x0D])))
        elif opcode == OP_DEVICE_CONTROL:  # control -> ACK + echo event
            await self._send(event_frame(0xB0, 0x00, frame.payload))
            addr = frame.payload[0] | (frame.payload[1] << 8)
            attr = frame.payload[2]
            await self._send(event_frame(OP_EVENT_NODE_STATUS, 0x06, bytes([addr & 0xFF, (addr >> 8) & 0xFF, attr, *frame.payload[3:]])))

    async def _send(self, frame: bytes) -> None:
        if self.writer is not None:
            self.writer.write(frame)
            await self.writer.drain()

    async def push_event(self, payload: bytes) -> None:
        """Unsolicited mesh event (e.g. physical panel press)."""
        await self._send(event_frame(OP_EVENT_NODE_STATUS, 0x06, payload))


class FakeHass:
    """Minimal hass surface used by the coordinator."""

    def __init__(self) -> None:
        self.loop = asyncio.get_event_loop()
        self.data: dict = {}

    def async_create_task(self, coro, name=None):
        return asyncio.ensure_future(coro)


class FakeEntry:
    def __init__(self) -> None:
        self.data = {"kind": "gateway", "connection_type": "tcp", "host": "127.0.0.1", "port": 4196}
        self.options: dict = {}
        self.entry_id = "test-entry"


async def main() -> None:
    mock = MockGateway()
    await mock.start()
    await asyncio.sleep(0.2)

    hass = FakeHass()
    entry = FakeEntry()
    coordinator = SymiMeshCoordinator(hass, entry)

    added: list = []
    coordinator.register_platform("switch", lambda entities: added.extend(entities))
    coordinator.register_platform("climate", lambda entities: added.extend(entities))

    await coordinator.async_setup()
    await asyncio.sleep(2.5)  # connect + device list + report enable + state query

    devices = {d.mac_address: d for d in coordinator.registry.all_devices()}
    check("发现 2 台设备", len(devices) == 2, f"got {len(devices)}")
    check("2路开关实体创建", len([e for e in added if type(e).__name__ == "SymiSwitch"]) == 2)
    check("温控器实体创建", len([e for e in added if type(e).__name__ == "SymiClimate"]) == 1)

    switches = [e for e in added if type(e).__name__ == "SymiSwitch"]
    if switches:
        sw = switches[0]
        check("unique_id 由 MAC 生成", sw.unique_id == "ccda20b84a89_switch_1", sw.unique_id)
        check("初始状态=开(0x06->ch1 on)", sw.is_on is True)
        check("第2路初始=关", switches[1].is_on is False)

    climate = [e for e in added if type(e).__name__ == "SymiClimate"]
    if climate:
        cl = climate[0]
        check("温控器当前温度 34.73", cl.current_temperature == 34.73, str(cl.current_temperature))

    # Unsolicited panel event: switch ch1 OFF (0x05 = 01 01)
    await mock.push_event(bytes([0x10, 0x01, 0x02, 0x05]))
    await asyncio.sleep(0.3)
    if switches:
        check("面板事件后 ch1=关", switches[0].is_on is False)
        check("面板事件后 ch2 保持关", switches[1].is_on is False)

    # Control command: turn ch1 ON via entity API
    await switches[0].async_turn_on()
    await asyncio.sleep(0.3)
    frames = b"".join(mock.received)
    check("控制帧已发送", bytes([0x30]) in frames, frames.hex())
    # 0x30 payload: addr LE, attr 0x02, state byte (2ch: ch1 on -> 0x06)
    check("控制帧内容正确", bytes([0x10, 0x01, 0x02, 0x06]) in frames)
    check("回显事件后 ch1=开", switches[0].is_on is True)

    # Three-in-one promotion: thermostat answers 0x68 -> fan entity expected
    fan_added: list = []
    coordinator.register_platform("fan", lambda entities: fan_added.extend(entities))
    await mock.push_event(bytes([0x00, 0x02, 0x68, 0x02]))
    await asyncio.sleep(0.3)
    check("三合一识别并创建新风实体", len(fan_added) == 1, f"got {len(fan_added)}")

    await coordinator.async_shutdown()
    await mock.stop()

    print(f"\n===== {len(PASS)} passed, {len(FAIL)} failed =====")
    if FAIL:
        for f in FAIL:
            print("FAILED:", f)
        sys.exit(1)


asyncio.run(main())
