#!/usr/bin/env python3
"""Symi Mesh serial<->TCP bridge.

Exposes the Symi mesh gateway (attached to the Mac's USB serial port) as a
plain TCP endpoint on port 4196, so the Home Assistant container can reach it
over the Docker network via host.docker.internal. All traffic is logged with
timestamps + hex for verification.
"""
import asyncio
import sys
import time

import serial_asyncio

SERIAL_PORT = "/dev/cu.wchusbserial8330"
BAUD_RATE = 115200
TCP_HOST = "0.0.0.0"
TCP_PORT = 4196

LOG_PATH = "/tmp/symi_bridge.log"


def _log(msg: str) -> None:
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with open(LOG_PATH, "a", buffering=1) as fh:
        fh.write(line + "\n")


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, tag: str) -> None:
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            _log(f"{tag} {data.hex(' ').upper()}")
            writer.write(data)
            await writer.drain()
    except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
        pass


async def handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    peer = writer.get_extra_info("peername")
    _log(f"TCP client connected: {peer}")
    sr = sw = None
    try:
        sr, sw = await asyncio.wait_for(
            serial_asyncio.open_serial_connection(url=SERIAL_PORT, baudrate=BAUD_RATE),
            timeout=5,
        )
        # Flush stale bytes left in the serial RX buffer from a previous
        # (interrupted) session, so we don't forward garbage to the client.
        try:
            while True:
                await asyncio.wait_for(sr.read(4096), timeout=0.2)
        except asyncio.TimeoutError:
            pass
        _log(f"serial opened: {SERIAL_PORT} @ {BAUD_RATE} (buffer flushed)")
    except Exception as exc:  # noqa: BLE001
        _log(f"serial open failed: {exc}")
        writer.close()
        return

    t1 = asyncio.create_task(_pipe(reader, sw, "TCP>SER"))
    t2 = asyncio.create_task(_pipe(sr, writer, "SER>TCP"))
    try:
        await asyncio.gather(t1, t2)
    finally:
        for t in (t1, t2):
            t.cancel()
        writer.close()
        _log("TCP client disconnected")


async def main() -> None:
    server = await asyncio.start_server(handle_client, TCP_HOST, TCP_PORT)
    _log(f"bridge listening on {TCP_HOST}:{TCP_PORT} -> {SERIAL_PORT}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
