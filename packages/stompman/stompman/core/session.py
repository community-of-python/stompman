# Session task entrypoints forward failures to the supervisor and pending receipts.
import asyncio
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import replace
from typing import Any, Protocol, cast, runtime_checkable
from uuid import uuid4

from stompman.config import ConnectionParameters, Heartbeat
from stompman.connection import AbstractConnection
from stompman.core._tasks import await_cleanup
from stompman.core.config import RuntimeConfig
from stompman.errors import (
    ConnectionConfirmationTimeout,
    ConnectionLostError,
    ReceiptRejectedError,
    ReceiptTimeoutError,
    StompProtocolConnectionIssue,
    UnsupportedProtocolVersion,
)
from stompman.frames import (
    AnyClientFrame,
    AnyServerFrame,
    ConnectedFrame,
    ConnectFrame,
    ConnectHeaders,
    DisconnectFrame,
    ErrorFrame,
    HeartbeatFrame,
    MessageFrame,
    ReceiptFrame,
)


@runtime_checkable
class _HeartbeatSender(Protocol):
    async def send_heartbeat(self) -> None: ...


class Session:
    """Own one transport, reader, write order and receipt namespace."""

    def __init__(self, connection: AbstractConnection, server: ConnectionParameters, config: RuntimeConfig) -> None:
        self.connection = connection
        self.server = server
        self.config = config
        self.generation = 0
        self.heartbeat = Heartbeat(0, 0)
        self.failed = asyncio.Event()
        self.failure: Exception | None = None
        self._frames = connection.read_frames()
        self._writes = asyncio.Lock()
        self._active_write: asyncio.Task[None] | None = None
        self._receipts: dict[str, asyncio.Future[ReceiptFrame]] = {}
        self._tasks: list[asyncio.Task[None]] = []
        self._connected_at = time.monotonic()
        self._last_received = self._connected_at
        self._last_message = self._connected_at
        self._last_sent = self._connected_at
        self._closed = False
        self._close_task: asyncio.Task[None] | None = None

    async def handshake(self) -> StompProtocolConnectionIssue | None:
        headers = cast(
            "ConnectHeaders",
            self.server.connect_headers
            | {
                "accept-version": "1.2",
                "host": self.server.host,
                "login": self.server.login,
                "passcode": self.server.unescaped_passcode,
                "heart-beat": self.config.heartbeat.to_header(),
            },
        )
        collected: list[MessageFrame | ReceiptFrame | ErrorFrame | HeartbeatFrame] = []
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            async with asyncio.timeout(self.config.connection_confirmation_timeout):
                await self.write(ConnectFrame(headers=headers))
                while True:
                    frame = await anext(self._frames)
                    if isinstance(frame, ConnectedFrame):
                        break
                    collected.append(frame)
                    if isinstance(frame, ErrorFrame):
                        return ConnectionConfirmationTimeout(
                            timeout=self.config.connection_confirmation_timeout, frames=collected
                        )
        except TimeoutError:
            return ConnectionConfirmationTimeout(timeout=self.config.connection_confirmation_timeout, frames=collected)
        except StopAsyncIteration as error:
            raise ConnectionLostError(reason="eof during handshake") from error
        version = frame.headers.get("version", "")
        if version != "1.2":
            return UnsupportedProtocolVersion(given_version=version, supported_version="1.2")
        server_heartbeat = Heartbeat.from_header(frame.headers.get("heart-beat", "0,0"))
        client_heartbeat = self.config.heartbeat
        self.heartbeat = Heartbeat(
            max(client_heartbeat.will_send_interval_ms, server_heartbeat.want_to_receive_interval_ms)
            if client_heartbeat.will_send_interval_ms and server_heartbeat.want_to_receive_interval_ms
            else 0,
            max(client_heartbeat.want_to_receive_interval_ms, server_heartbeat.will_send_interval_ms)
            if client_heartbeat.want_to_receive_interval_ms and server_heartbeat.will_send_interval_ms
            else 0,
        )
        self._last_received = self._last_message = time.monotonic()
        return None

    def start(self, receive: Callable[[AnyServerFrame, "Session"], None]) -> None:
        self._tasks.append(asyncio.create_task(self._read(receive), name="stomp-reader"))
        if self.heartbeat.want_to_receive_interval_ms:
            self._tasks.append(asyncio.create_task(self._monitor(), name="stomp-heartbeat"))
        if self.heartbeat.will_send_interval_ms:
            self._tasks.append(asyncio.create_task(self._send_heartbeats(), name="stomp-heartbeat-sender"))
        if self.config.no_message_restart_interval is not None:
            self._tasks.append(asyncio.create_task(self._monitor_idle(), name="stomp-idle"))

    def fail(self, error: Exception) -> None:
        if not self.failed.is_set():
            self.failure = error
            self.failed.set()
            if self._active_write is not None and not self._active_write.done():
                self._active_write.cancel()
            for future in self._receipts.values():
                if not future.done():
                    future.set_exception(ConnectionLostError(reason=error))

    async def _read(self, receive: Callable[[AnyServerFrame, "Session"], None]) -> None:
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            async for frame in self._frames:
                self._last_received = time.monotonic()
                self._resolve_receipt(frame)
                if isinstance(frame, MessageFrame):
                    self._last_message = self._last_received
                receive(frame, self)
                if self.failed.is_set():
                    return
            self.fail(ConnectionLostError(reason="eof"))
        except Exception as error:  # ruff: ignore[blind-except]
            self.fail(error)

    def _resolve_receipt(self, frame: AnyServerFrame) -> None:
        if isinstance(frame, ReceiptFrame):
            future = self._receipts.pop(frame.headers["receipt-id"], None)
            if future is not None and not future.done():
                future.set_result(frame)
        elif isinstance(frame, ErrorFrame):
            receipt_id = frame.headers.get("receipt-id")
            for pending_id, pending in list(self._receipts.items()):
                if receipt_id is None or receipt_id == pending_id:
                    self._receipts.pop(pending_id)
                    if not pending.done():
                        pending.set_exception(ReceiptRejectedError(receipt_id=pending_id, frame=frame))

    def is_alive(self) -> bool:
        if self._closed or self.failed.is_set():
            return False
        receive_ms = self.heartbeat.want_to_receive_interval_ms
        if not receive_ms:
            return True
        idle = time.monotonic() - self._last_received
        # Custom transports expose wall-clock last_read_time. Count partial frames too.
        if self.connection.last_read_time is not None:
            idle = min(idle, max(0, time.time() - self.connection.last_read_time))
        return idle < receive_ms / 1000 * self.config.check_server_alive_interval_factor

    async def _monitor(self) -> None:
        interval = self.heartbeat.want_to_receive_interval_ms / 1000
        try:
            while not self.failed.is_set():
                await asyncio.sleep(interval)
                if not self.is_alive():
                    self.fail(ConnectionLostError(reason="negotiated receive heartbeat expired"))
                    return
        except Exception as error:  # ruff: ignore[blind-except]
            self.fail(error)

    async def _send_heartbeats(self) -> None:
        interval = self.heartbeat.will_send_interval_ms / 1000
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            while not self.failed.is_set():
                await asyncio.sleep(interval)
                async with self._writes:
                    if self.failed.is_set() or self._closed:
                        return
                    if time.monotonic() - self._last_sent < interval:
                        continue
                    if isinstance(self.connection, _HeartbeatSender):
                        await self._write_transport(self.connection.send_heartbeat())
                    else:
                        self.connection.write_heartbeat()
                    self._last_sent = time.monotonic()
        except Exception as error:  # ruff: ignore[blind-except]
            self.fail(error)

    async def _monitor_idle(self) -> None:
        interval = self.config.no_message_restart_interval
        assert interval is not None  # ruff: ignore[assert]
        seconds = interval.total_seconds()
        while not self.failed.is_set():
            await asyncio.sleep(max(0, seconds - (time.monotonic() - self._last_message)))
            if time.monotonic() - self._last_message >= seconds:
                self.fail(ConnectionLostError(reason="no messages received within timeout"))
                return

    async def _write_transport(self, operation: Coroutine[Any, Any, None]) -> None:
        """Separate caller cancellation from watchdog-driven transport cancellation."""
        caller = asyncio.current_task()
        assert caller is not None  # ruff: ignore[assert]
        cancelling = caller.cancelling()
        task = self._active_write = asyncio.create_task(operation, name="stomp-write")
        try:
            await task
        except asyncio.CancelledError as error:
            if self.failed.is_set() and caller.cancelling() == cancelling:
                raise ConnectionLostError(reason=self.failure or "session failed during write") from error
            self.fail(ConnectionLostError(reason="transport write was cancelled"))
            raise
        finally:
            self._active_write = None

    async def write(
        self, frame: AnyClientFrame, *, receipt_timeout: float | None = None, receipt_id: str = ""
    ) -> ReceiptFrame | None:
        future: asyncio.Future[ReceiptFrame] | None = None
        written = False
        if receipt_timeout is not None:
            receipt_id = receipt_id or str(uuid4())
            frame = replace(frame, headers=frame.headers | {"receipt": receipt_id})  # type: ignore[arg-type]
            future = asyncio.get_running_loop().create_future()
            self._receipts[receipt_id] = future
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            async with asyncio.timeout(receipt_timeout):
                async with self._writes:
                    if self._closed or self.failed.is_set():
                        raise ConnectionLostError(reason="session is closed")  # ruff: ignore[raise-within-try]
                    await self._write_transport(self.connection.write_frame(frame))
                    written = True
                    self._last_sent = time.monotonic()
                if future is not None:
                    return await future
        except TimeoutError as error:
            if written and future is not None and future.done() and not future.cancelled():
                return future.result()
            assert receipt_timeout is not None  # ruff: ignore[assert]
            raise ReceiptTimeoutError(receipt_id=receipt_id, timeout=receipt_timeout) from error
        except ConnectionLostError as error:
            self.fail(error)
            raise
        finally:
            if future is not None:
                self._receipts.pop(receipt_id, None)
                if future.done() and not future.cancelled():
                    future.exception()
                else:
                    future.cancel()
        return None

    async def close(self, *, graceful: bool = False) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close(graceful=graceful), name="stomp-close")
        await await_cleanup(self._close_task)

    async def _close(self, *, graceful: bool) -> None:
        try:
            if graceful and not self.failed.is_set():
                with suppress(ConnectionLostError, ReceiptTimeoutError, TimeoutError):
                    async with asyncio.timeout(self.config.disconnect_confirmation_timeout):
                        await self.write(
                            DisconnectFrame(headers={}), receipt_timeout=self.config.disconnect_confirmation_timeout
                        )
        finally:
            self._closed = True
            self.fail(ConnectionLostError(reason="session closed"))
            for task in self._tasks:
                task.cancel()
            tasks = [*self._tasks]
            if self._active_write is not None:
                tasks.append(self._active_write)
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
                with suppress(ConnectionLostError, OSError):
                    await self._frames.aclose()
            finally:
                with suppress(ConnectionLostError, OSError):
                    await self.connection.close()
