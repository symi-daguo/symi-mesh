"""Transport layer for the Symi Mesh gateway.

asyncio-based TCP (default port 4196) and serial (pyserial) connections with
exponential-backoff reconnection and a paced command queue (the 50 ms frame
spacing from the Node-RED implementation is kept as the default interval, but
is configurable and no longer blocks the parser).
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any, Awaitable, Callable

from .const import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_RECONNECT_INTERVAL,
    MAX_RECONNECT_INTERVAL,
)
from .protocol import ProtocolHandler

_LOGGER = logging.getLogger(__name__)

# serialx 是 HA 2026+ 上 pyserial-asyncio 的替代。它首次 import 会连带拉起
# google.protobuf（几十毫秒），无论放在模块顶层还是懒加载，只要发生在事件
# 循环线程里就会被 HA 记成 "Detected blocking call"。因此统一走 executor
# 解析一次并缓存：模块顶层不 import，回调里也不直接 import。
_OPEN_SERIAL_CALLABLE: Callable[..., Any] | None = None


def _import_open_serial_connection() -> Callable[..., Any]:
    try:
        from serialx.async_serial import open_serial_connection
    except ImportError:  # pragma: no cover - HA < 2025
        from serial_asyncio import open_serial_connection
    return open_serial_connection


async def _async_open_serial_callable() -> Callable[..., Any]:
    global _OPEN_SERIAL_CALLABLE
    if _OPEN_SERIAL_CALLABLE is None:
        loop = asyncio.get_running_loop()
        _OPEN_SERIAL_CALLABLE = await loop.run_in_executor(
            None, _import_open_serial_connection
        )
    return _OPEN_SERIAL_CALLABLE

FrameCallback = Callable[[object], None]


class GatewayTransport:
    """Owns the socket/serial link and the outbound command queue."""

    def __init__(
        self,
        connection_type: str,
        host: str | None = None,
        port: int = 4196,
        serial_port: str | None = None,
        baud_rate: int = 115200,
        frame_interval: float = 0.05,
        reconnect_interval: int = DEFAULT_RECONNECT_INTERVAL,
    ) -> None:
        self.connection_type = connection_type
        self.host = host
        self.port = port
        self.serial_port = serial_port
        self.baud_rate = baud_rate
        self.frame_interval = frame_interval
        self.reconnect_interval = reconnect_interval

        self.protocol = ProtocolHandler()
        self.connected = False
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._read_task: asyncio.Task | None = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._frame_callback: FrameCallback | None = None
        self._connect_callback: Callable[[], Awaitable[None]] | None = None
        self._disconnect_callback: Callable[[], Awaitable[None]] | None = None
        self._reconnect_attempts = 0
        # 首次连接是否成功由配置条目设置阶段（test-before-setup）等待。
        self._connected_event = asyncio.Event()

    # -- callbacks ----------------------------------------------------------

    def set_frame_callback(self, callback: FrameCallback | None) -> None:
        self._frame_callback = callback

    def set_connection_callbacks(
        self,
        on_connect: Callable[[], Awaitable[None]] | None,
        on_disconnect: Callable[[], Awaitable[None]] | None,
    ) -> None:
        self._connect_callback = on_connect
        self._disconnect_callback = on_disconnect

    # -- lifecycle ----------------------------------------------------------

    async def async_start(self) -> None:
        """Start the transport; reconnects forever until async_stop."""
        self._stop_event.clear()
        while not self._stop_event.is_set():
            try:
                await self._async_connect_once()
                if self._connect_callback is not None:
                    await self._connect_callback()
                self._reconnect_attempts = 0
                await self._read_loop()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - logged, retried
                _LOGGER.debug("Gateway connection error: %s", err)
            finally:
                await self._async_teardown_link()
                if self.connected and self._disconnect_callback is not None:
                    self.connected = False
                    await self._disconnect_callback()

            if self._stop_event.is_set():
                break
            self._reconnect_attempts += 1
            delay = min(
                self.reconnect_interval * (2 ** min(self._reconnect_attempts - 1, 4)),
                MAX_RECONNECT_INTERVAL,
            )
            if self._reconnect_attempts == 1:
                _LOGGER.info("Gateway unreachable, retrying in %ss", delay)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=delay)

    async def async_stop(self) -> None:
        self._stop_event.set()
        await self._async_teardown_link()

    async def async_wait_connected(self, timeout: float) -> bool:
        """Wait for the first successful link; used by test-before-setup."""
        try:
            await asyncio.wait_for(self._connected_event.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return True

    # -- connection ---------------------------------------------------------

    async def _async_connect_once(self) -> None:
        if self.connection_type == "serial":
            await self._async_connect_serial()
        else:
            await self._async_connect_tcp()

    async def _async_connect_tcp(self) -> None:
        _LOGGER.debug("Connecting to gateway %s:%s", self.host, self.port)
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=DEFAULT_CONNECT_TIMEOUT,
        )
        self.connected = True
        self._connected_event.set()
        _LOGGER.info("Connected to Symi gateway at %s:%s", self.host, self.port)

    async def _async_connect_serial(self) -> None:
        open_serial_connection = await _async_open_serial_callable()
        _LOGGER.debug("Opening serial port %s @ %d", self.serial_port, self.baud_rate)
        self._reader, self._writer = await asyncio.wait_for(
            open_serial_connection(url=self.serial_port, baudrate=self.baud_rate),
            timeout=DEFAULT_CONNECT_TIMEOUT,
        )
        self.connected = True
        self._connected_event.set()
        _LOGGER.info("Opened serial port %s at %d baud", self.serial_port, self.baud_rate)

    async def _async_teardown_link(self) -> None:
        if self._read_task and not self._read_task.done():
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._read_task
        self._read_task = None

        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
            self._writer = None
        self._reader = None
        self.connected = False
        self._connected_event.clear()
        self.protocol.clear_buffer()
        # Drop queued commands that will never be sent.
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                self._queue.get_nowait()

    # -- IO loops -----------------------------------------------------------

    async def _read_loop(self) -> None:
        if self.connection_type == "serial":
            await self._read_loop_serial()
            return
        assert self._reader is not None
        while not self._stop_event.is_set():
            data = await self._reader.read(4096)
            if not data:
                raise ConnectionError("Gateway closed the connection")
            self._handle_data(data)

    async def _read_loop_serial(self) -> None:
        assert self._reader is not None
        while not self._stop_event.is_set():
            data = await self._reader.read(4096)
            if not data:
                raise ConnectionError("Serial port closed")
            self._handle_data(data)

    def _handle_data(self, data: bytes) -> None:
        try:
            frames = self.protocol.add_data(data)
        except Exception:  # noqa: BLE001 - parser must never kill the loop
            _LOGGER.exception("Frame parsing failed")
            return
        for frame in frames:
            if self._frame_callback is not None:
                self._frame_callback(frame)

    # -- commands -----------------------------------------------------------

    async def async_send_frame(self, frame: bytes, priority: int = 1) -> None:
        """Queue a frame; priority 0 jumps the queue."""
        if priority == 0:
            # Re-insert at the front via an internal deque-like swap.
            items: list[bytes] = []
            while not self._queue.empty():
                items.append(self._queue.get_nowait())
            await self._queue.put(frame)
            for item in items:
                await self._queue.put(item)
        else:
            await self._queue.put(frame)
        self._ensure_worker()

    def _ensure_worker(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._queue_worker())

    async def _queue_worker(self) -> None:
        while True:
            frame = await self._queue.get()
            if not self.connected or self._writer is None:
                continue
            try:
                self._writer.write(frame)
                await self._writer.drain()
            except Exception as err:  # noqa: BLE001 - drop frame, keep loop
                _LOGGER.debug("Frame write failed: %s", err)
            await asyncio.sleep(self.frame_interval)
