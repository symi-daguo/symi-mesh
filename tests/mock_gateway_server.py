"""Standalone minimal Symi gateway for multi-gateway verification (container)."""
import asyncio

CHECK = lambda b: __import__("functools").reduce(lambda a, x: a ^ x, b, 0)  # noqa: E731
EVENTS = []


def frame(opcode: int, status: int, payload: bytes) -> bytes:
    head = bytes([0x53, opcode, status, len(payload)])
    return head + payload + bytes([CHECK(head + payload)])


def dev_list(index: int, mac: bytes, addr: int, dtype: int, sub: int) -> bytes:
    payload = bytes([3, index, *mac, addr & 0xFF, (addr >> 8) & 0xFF, 0xA8, 0x01, dtype, sub, 0x01, 0x00])
    return frame(0x92, 0x00, payload)


def parse_frames(buf: bytearray):
    out = []
    while True:
        i = bytes(buf).find(b"\x53")
        if i < 0 or len(buf) - i < 5:
            break
        length = buf[i + 3]
        if len(buf) - i < 5 + length:
            break
        out.append(bytes(buf[i : i + 5 + length]))
        del buf[: i + 5 + length]
    return out


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    # Two synthetic devices: 1-ch switch (MAC aa..01, addr 0x0300) + curtain (aa..02, 0x0301)
    writer.write(dev_list(0, bytes.fromhex("AAAAAA000001"), 0x0300, 1, 1))
    writer.write(dev_list(1, bytes.fromhex("AAAAAA000002"), 0x0301, 5, 2))
    writer.write(dev_list(2, bytes.fromhex("AAAAAA000003"), 0x0302, 7, 1))
    # 连接即推送窗帘多组合状态（0x05=停止 0x06=位置0）——验证 0x80 多组合解析
    writer.write(frame(0x80, 0x06, bytes([0x01, 0x03, 0x05, 0x03, 0x06, 0x00])))
    await writer.drain()
    buf = bytearray()
    while True:
        try:
            data = await reader.read(4096)
        except Exception:
            return
        if not data:
            return
        buf.extend(data)
        for fr in parse_frames(buf):
            op, payload = fr[1], fr[4:-1]
            if op == 0x12:
                writer.write(dev_list(0, bytes.fromhex("AAAAAA000001"), 0x0300, 1, 1))
                writer.write(dev_list(1, bytes.fromhex("AAAAAA000002"), 0x0301, 5, 2))
                writer.write(dev_list(2, bytes.fromhex("AAAAAA000003"), 0x0302, 7, 1))
            elif op == 0x10:
                writer.write(frame(0xB0, 0x00, payload))
            elif op == 0x32:
                writer.write(frame(0xB2, 0x00, payload))
                # 窗帘状态上报：运行=停止(3) 位置=0
                writer.write(frame(0x80, 0x06, bytes([payload[0], payload[1], 0x05, 0x03])))
                writer.write(frame(0x80, 0x06, bytes([payload[0], payload[1], 0x06, 0x00])))
            elif op == 0x30:
                writer.write(frame(0xB0, 0x00, payload))
                writer.write(frame(0x80, 0x06, bytes([payload[0], payload[1], payload[2], *payload[3:]])))
                # 窗帘运行联动位置（真实设备行为）：开→100 关→0，位置变化才产生 HA 事件
                if payload[2] == 0x05 and payload[3] in (1, 2):
                    pos = 100 if payload[3] == 1 else 0
                    writer.write(frame(0x80, 0x06, bytes([payload[0], payload[1], 0x06, pos])))
            await writer.drain()


async def main() -> None:
    server = await asyncio.start_server(handle, "0.0.0.0", 4197)
    print("mock gateway #2 on 4197", flush=True)
    async with server:
        await server.serve_forever()


asyncio.run(main())
