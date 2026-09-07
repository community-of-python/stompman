"""One ready STOMP connection. Transport writes and receipt waits are separate phases."""

import asyncio
import math
import time
from collections.abc import Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Literal

from ._tasks import Cancellation, await_cleanup
from .acknowledgement import Acknowledgement
from .command import Command, Commands
from .config import DEFAULT_CONFIRMATION, Confirmation, ConnectionSettings, Heartbeat
from .errors import (
    BrokerError,
    ConnectionLostError,
    ReceiptRejectedError,
    ReceiptTimeoutError,
)
from .frames import (
    AckMode,
    AnyClientFrame,
    AnyServerFrame,
    DisconnectFrame,
    ErrorFrame,
    MessageFrame,
    ReceiptFrame,
)
from .handshake import NegotiatedConnection
from .receipts import Receipts, ignore_rejection
from .transport import Transport


class SessionPhase(Enum):
    OPEN = auto()
    DISCONNECT_SENT = auto()
    TERMINAL = auto()


@dataclass(frozen=True, slots=True)
class Closing:
    task: asyncio.Task[None]


class Session:
    """Constructed only after handshake, with a permanent generation and transport."""

    def __init__(
        self,
        connection: NegotiatedConnection,
        settings: ConnectionSettings,
        generation: int,
        receive: Callable[[AnyServerFrame, "Session"], None],
    ) -> None:
        self._connection = connection
        self.generation = generation
        self.ended: asyncio.Future[Exception] = asyncio.get_running_loop().create_future()
        self._commands = Commands()
        self._settings = settings
        self._writes = asyncio.Lock()
        self._writing: set[asyncio.Task[None]] = set()
        self._tasks: list[asyncio.Task[None]] = []
        self._close_state: Literal[SessionPhase.OPEN] | Closing = SessionPhase.OPEN
        self._outbound = SessionPhase.OPEN
        self._last_received = self._last_message = self._last_sent = time.monotonic()

        self._tasks.append(asyncio.create_task(self._read(receive), name="stomp-reader"))
        if self.heartbeat.want_to_receive_interval_ms or math.isfinite(self._settings.idle_timeout):
            self._tasks.append(asyncio.create_task(self._monitor(), name="stomp-watchdog"))
        if self.heartbeat.will_send_interval_ms:
            self._tasks.append(asyncio.create_task(self._send_heartbeats(), name="stomp-heartbeat"))

    @property
    def transport(self) -> Transport:
        return self._connection.transport

    @property
    def heartbeat(self) -> Heartbeat:
        return self._connection.heartbeat

    def fail(self, error: Exception) -> None:
        if not self.ended.done():
            self.ended.set_result(error)
            self.receipts.fail(error)
            for task in self._writing:
                task.cancel()
            if not isinstance(self._close_state, Closing):
                self._close_state = Closing(asyncio.create_task(self._close(graceful=False), name="stomp-close"))

    @property
    def receipts(self) -> Receipts:
        return self._commands.receipts

    @property
    def writing(self) -> bool:
        return self._writes.locked()

    def check(self) -> None:
        if self.ended.done():
            raise ConnectionLostError(reason=self.ended.result())
        if self._outbound is SessionPhase.TERMINAL:
            raise ConnectionLostError(reason="broker ended session")

    def is_alive(self) -> bool:
        if self.ended.done() or self._outbound is SessionPhase.TERMINAL:
            return False
        last_received = max(self._last_received, self.transport.last_received_at)
        receive = self.heartbeat.want_to_receive_interval_ms / 1000
        return not receive or time.monotonic() - last_received < receive * self._settings.heartbeat_tolerance

    def validate_delivery(self, frame: MessageFrame, ack: AckMode) -> None:
        self._connection.protocol.delivery(frame, ack)

    def acknowledgement(
        self,
        frame: MessageFrame,
        subscription_id: str,
        confirmation: Confirmation,
    ) -> Acknowledgement:
        return self._connection.protocol.acknowledgement(frame, self, subscription_id, confirmation)

    async def _read(self, receive: Callable[[AnyServerFrame, "Session"], None]) -> None:
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            async for frame in self._connection.frames:
                self._last_received = time.monotonic()
                self._connection.protocol.incoming(frame)
                if isinstance(frame, ErrorFrame):
                    self._broker_error(frame, receive)
                    return
                if isinstance(frame, ReceiptFrame):
                    self.receipts.receive(frame)
                if isinstance(frame, MessageFrame):
                    self._last_message = self._last_received
                receive(frame, self)
                if self.ended.done():
                    return
            self.fail(ConnectionLostError(reason="eof"))
        except Exception as error:  # ruff: ignore[blind-except]
            self.fail(error)

    def _broker_error(self, frame: ErrorFrame, receive: Callable[[AnyServerFrame, "Session"], None]) -> None:
        failure = ConnectionLostError(reason=BrokerError(frame=frame))
        self._outbound = SessionPhase.TERMINAL
        self.receipts.reject(frame, failure)
        receive(frame, self)
        self.fail(failure)

    async def _transport_write(self, operation: Coroutine[Any, Any, None]) -> None:
        cancellation = Cancellation.capture()
        task = asyncio.create_task(operation, name="stomp-write")
        self._writing.add(task)
        try:
            await task
        except asyncio.CancelledError as error:
            if self.ended.done() and not cancellation.requested:
                raise ConnectionLostError(reason=self.ended.result()) from error
            self.fail(ConnectionLostError(reason="transport write was cancelled"))
            raise
        except (ConnectionLostError, OSError) as error:
            self.fail(error)
            raise ConnectionLostError(reason=error) from error
        finally:
            self._writing.discard(task)

    async def transmit(self, frame: AnyClientFrame) -> None:
        async with self._writes:
            self.check()
            if self._outbound is SessionPhase.DISCONNECT_SENT:
                raise ConnectionLostError(reason="DISCONNECT already sent")
            self._connection.protocol.outgoing(frame)
            if isinstance(frame, DisconnectFrame):
                self._outbound = SessionPhase.DISCONNECT_SENT
            await self._transport_write(self.transport.write_frame(frame))
            self._last_sent = time.monotonic()

    def command(
        self,
        frame: AnyClientFrame,
        confirmation: Confirmation = DEFAULT_CONFIRMATION,
        rejected: Callable[[ReceiptRejectedError], None] = ignore_rejection,
    ) -> Command:
        self.check()
        self._connection.protocol.outgoing(frame)
        return self._commands.start(self, frame, confirmation, rejected)

    async def submit(self, frame: AnyClientFrame, confirmation: Confirmation) -> Command:
        command = self.command(frame, confirmation)
        await command.submit()
        return command

    async def write(
        self, frame: AnyClientFrame, confirmation: Confirmation = DEFAULT_CONFIRMATION
    ) -> ReceiptFrame | None:
        return await self.command(frame, confirmation).complete()

    async def _monitor(self) -> None:
        heartbeat_interval = self.heartbeat.want_to_receive_interval_ms / 1000 or math.inf
        while not self.ended.done():
            idle_remaining = self._last_message + self._settings.idle_timeout - time.monotonic()
            await asyncio.sleep(max(0, min(heartbeat_interval, idle_remaining)))
            if not self.is_alive():
                self.fail(ConnectionLostError(reason="negotiated receive heartbeat expired"))
            elif time.monotonic() - self._last_message >= self._settings.idle_timeout:
                self.fail(ConnectionLostError(reason="no messages received within timeout"))

    async def _send_heartbeats(self) -> None:
        interval = self.heartbeat.will_send_interval_ms / 1000
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            while not self.ended.done():
                await asyncio.sleep(interval)
                async with self._writes:
                    if self.ended.done() or self._outbound is SessionPhase.DISCONNECT_SENT:
                        return
                    if time.monotonic() - self._last_sent >= interval:
                        await self._transport_write(self.transport.send_heartbeat())
                        self._last_sent = time.monotonic()
        except Exception as error:  # ruff: ignore[blind-except]
            self.fail(error)

    async def close(self, *, graceful: bool = False) -> None:
        if not isinstance(self._close_state, Closing):
            self._close_state = Closing(asyncio.create_task(self._close(graceful=graceful), name="stomp-close"))
        await await_cleanup(self._close_state.task)

    async def _close(self, *, graceful: bool) -> None:
        try:
            if graceful and not self.ended.done():
                with suppress(ConnectionLostError, ReceiptRejectedError, ReceiptTimeoutError):
                    await self.write(DisconnectFrame(headers={}), self._settings.disconnect)
        finally:
            self.fail(ConnectionLostError(reason="session closed"))
            await self._commands.close()
            for task in self._tasks:
                task.cancel()
            try:
                await asyncio.gather(*self._tasks, *self._writing, return_exceptions=True)
            finally:
                await self._connection.close()
