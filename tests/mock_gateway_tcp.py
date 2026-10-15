#!/usr/bin/env python3
"""Standalone mock Symi gateway for multi-gateway architecture verification.

Listens on 0.0.0.0:4197 and serves 2 simulated devices with distinct MACs:
  - type 0x04 dimmable light, MAC 11:22:33:44:55:66 @ 0x0200
  - type 0x03 smart socket,  MAC 77:88:99:AA:BB:CC @ 0x0201
Replies to 0x12 (list), 0x30 (control), 0x10 (enable report), 0x32 (query).
"""
import asyncio
import time

LOG = "/tmp/symi_mock2.log"


def log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG, "a", buffering=1) as fh:
        fh.write(line + "\n")


def frame(opcode: int, status: int, payload: bytes) -> bytes:
    head = bytes([0x53, opcode, status, len(payload)])
    return head + payload + bytes([(0)])


def cksum(data: bytes) -> int:
    c = 0
    for b in data:
        c ^= b
    return c


def build(opcode: int, status: int, payload: bytes) -> bytes:
    head = bytes([0x53, opcode, status, len(payload)])
    return head + payload + bytes([cksum(head + payload)])


def device_frames() -> bytes:
    out = b""
    for index, (mac, addr, dtype, sub) in enumerate(
        [(bytes.fromhex("112233445566"), 0x0200, 0x04, 0x00),
         (bytes.fromhex("778899AABBCC"), 0x0201, 0x03, 0x00)]
    ):
        payload = bytes(
            [2, index, *mac, addr & 0xFF, (addr >> 8) & 0xFF, 0xA8, 0x01,
             dtype, sub, 0x01, 0x00]
        )
        out += build(0x92, 0x00, payload)
    return out


async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, tag: str) -> None:
    while True:
        data = await reader.read(4096)
        if not data:
            break
        log(f"{tag} {data.hex(' ').upper()}")
        if b"\x53\x12" in data:
            writer.write(device_frames())
            await writer.drain()
        else:
            # echo a B0 ack for any control/report-enable frame
            ack = build(0xB0, 0x00, b"")
            writer.write(ack)
            await writer.drain()


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    log("client connected")
    try:
        await pump(reader, writer, "TCP>")
    except (ConnectionResetError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()
        log("client gone")


async def main() -> None:
    server = await asyncio.start_server(handle, "0.0.0.0", 4197)
    log("mock gateway #2 listening on 0.0.0.0:4197")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
