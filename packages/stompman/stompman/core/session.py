"""One ready STOMP connection. Transport writes and receipt waits are separate phases."""

import asyncio
import math
import time
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import Any, cast

from ._tasks import await_cleanup
from .config import DEFAULT_CONFIRMATION, Confirmation, Confirmed, ConnectionSettings, Heartbeat, Server, Unconfirmed
from .errors import (
    ConnectionConfirmationTimeout,
    ConnectionLostError,
    ReceiptRejectedError,
    ReceiptTimeoutError,
    StompProtocolConnectionIssue,
    UnsupportedProtocolVersion,
)
from .frames import (
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
from .receipts import Receipt, Receipts, ignore_rejection
from .transport import Transport


@dataclass(kw_only=True)
class HandshakeFailedError(Exception):
    issue: StompProtocolConnectionIssue


async def handshake(
    transport: Transport, server: Server, settings: ConnectionSettings, frames: AsyncGenerator[AnyServerFrame, None]
) -> Heartbeat:
    headers = cast(
        "ConnectHeaders",
        dict(server.connect_headers)
        | {
            "accept-version": "1.2",
            "host": server.host,
            "login": server.login,
            "passcode": server.passcode,
            "heart-beat": settings.heartbeat.to_header(),
        },
    )
    collected: list[MessageFrame | ReceiptFrame | ErrorFrame | HeartbeatFrame] = []
    try:  # ruff: ignore[too-many-statements-in-try-clause]
        async with asyncio.timeout(settings.handshake_timeout):
            await transport.write_frame(ConnectFrame(headers=headers))
            async for frame in frames:
                if isinstance(frame, ConnectedFrame):
                    version = frame.headers.get("version", "")
                    if version != "1.2":
                        raise HandshakeFailedError(
                            issue=UnsupportedProtocolVersion(given_version=version, supported_version="1.2")
                        )
                    return settings.heartbeat.negotiate(Heartbeat.from_header(frame.headers.get("heart-beat", "0,0")))
                collected.append(frame)
                if isinstance(frame, ErrorFrame):
                    break
    except TimeoutError:
        pass
    raise HandshakeFailedError(
        issue=ConnectionConfirmationTimeout(timeout=settings.handshake_timeout, frames=collected)
    )


class CommandPhase(Enum):
    PREPARED = auto()
    SUBMITTING = auto()
    SUBMITTED = auto()
    CLOSED = auto()


class Command:
    """Own a receipt reservation from before submission until both phases finish."""

    def __init__(
        self,
        session: "Session",
        frame: AnyClientFrame,
        confirmation: Confirmation,
        rejected: Callable[[ReceiptRejectedError], None],
    ) -> None:
        self._session = session
        self._phase = CommandPhase.PREPARED
        self._confirmation = confirmation
        self._receipt: Receipt | Unconfirmed
        if isinstance(confirmation, Confirmed):
            self._receipt = session.receipts.reserve(rejected)
            self._frame = replace(frame, headers=frame.headers | {"receipt": self._receipt.id})  # type: ignore[arg-type]
            self._deadline = asyncio.get_running_loop().time() + confirmation.timeout
        else:
            self._receipt = confirmation
            self._frame = frame
            self._deadline = math.inf

    def _timeout(self) -> ReceiptTimeoutError:
        if not isinstance(self._receipt, Receipt) or not isinstance(self._confirmation, Confirmed):
            msg = "only a confirmed command can time out"
            raise TypeError(msg)
        return ReceiptTimeoutError(receipt_id=self._receipt.id, timeout=self._confirmation.timeout)

    async def submit(self) -> None:
        if self._phase is not CommandPhase.PREPARED:
            msg = "command has already been submitted or closed"
            raise RuntimeError(msg)
        self._phase = CommandPhase.SUBMITTING
        try:
            async with asyncio.timeout_at(self._deadline):
                await self._session.transmit(self._frame)
            self._phase = CommandPhase.SUBMITTED
        except TimeoutError as error:
            raise self._timeout() from error

    async def complete(self) -> ReceiptFrame | None:
        if self._phase is not CommandPhase.SUBMITTED:
            msg = "command must be submitted before completion"
            raise RuntimeError(msg)
        if isinstance(self._receipt, Unconfirmed):
            return None
        try:
            async with asyncio.timeout_at(self._deadline):
                return await self._receipt.result
        except TimeoutError as error:
            raise self._timeout() from error

    def invalidate(self, reason: str) -> None:
        self._session.fail(ConnectionLostError(reason=reason))

    def close(self) -> None:
        self._phase = CommandPhase.CLOSED
        if isinstance(self._receipt, Receipt):
            self._session.receipts.discard(self._receipt)


class SessionPhase(Enum):
    OPEN = auto()


@dataclass(frozen=True, slots=True)
class Closing:
    task: asyncio.Task[None]


class Session:
    """Constructed only after handshake, with a permanent generation and transport."""

    def __init__(
        self,
        transport: Transport,
        frames: AsyncGenerator[AnyServerFrame, None],
        settings: ConnectionSettings,
        generation: int,
        heartbeat: Heartbeat,
    ) -> None:
        self.transport = transport
        self.generation = generation
        self.heartbeat = heartbeat
        self.ended: asyncio.Future[Exception] = asyncio.get_running_loop().create_future()
        self.receipts = Receipts()
        self._frames = frames
        self._settings = settings
        self._writes = asyncio.Lock()
        self._writing: set[asyncio.Task[None]] = set()
        self._tasks: list[asyncio.Task[None]] = []
        self._close_state: SessionPhase | Closing = SessionPhase.OPEN
        self._last_received = self._last_message = self._last_sent = time.monotonic()

    @classmethod
    async def open(
        cls, transport: Transport, server: Server, settings: ConnectionSettings, generation: int
    ) -> "Session":
        frames = transport.read_frames()

        async def cleanup() -> None:
            try:
                await frames.aclose()
            finally:
                await transport.close()

        try:
            heartbeat = await handshake(transport, server, settings, frames)
        except BaseException:
            await await_cleanup(asyncio.create_task(cleanup()))
            raise
        return cls(transport, frames, settings, generation, heartbeat)

    def start(self, receive: Callable[[AnyServerFrame, "Session"], None]) -> None:
        self._tasks.append(asyncio.create_task(self._read(receive), name="stomp-reader"))
        if self.heartbeat.want_to_receive_interval_ms:
            self._tasks.append(asyncio.create_task(self._monitor(), name="stomp-watchdog"))
        if self.heartbeat.will_send_interval_ms:
            self._tasks.append(asyncio.create_task(self._send_heartbeats(), name="stomp-heartbeat"))
        if math.isfinite(self._settings.idle_timeout):
            self._tasks.append(asyncio.create_task(self._monitor_idle(), name="stomp-idle"))

    def fail(self, error: Exception) -> None:
        if not self.ended.done():
            self.ended.set_result(error)
            self.receipts.fail(error)
            for task in self._writing:
                task.cancel()

    @property
    def writing(self) -> bool:
        return self._writes.locked()

    def check(self) -> None:
        if self.ended.done():
            raise ConnectionLostError(reason=self.ended.result())

    def is_alive(self) -> bool:
        if self.ended.done():
            return False
        last_received = max(self._last_received, self.transport.last_received_at)
        receive = self.heartbeat.want_to_receive_interval_ms / 1000
        return not receive or time.monotonic() - last_received < receive * self._settings.heartbeat_tolerance

    async def _read(self, receive: Callable[[AnyServerFrame, "Session"], None]) -> None:
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            async for frame in self._frames:
                self._last_received = time.monotonic()
                if isinstance(frame, (ReceiptFrame, ErrorFrame)):
                    self.receipts.receive(frame)
                if isinstance(frame, MessageFrame):
                    self._last_message = self._last_received
                receive(frame, self)
                if self.ended.done():
                    return
            self.fail(ConnectionLostError(reason="eof"))
        except Exception as error:  # ruff: ignore[blind-except]
            self.fail(error)

    async def _transport_write(self, operation: Coroutine[Any, Any, None]) -> None:
        caller = asyncio.current_task()
        assert caller is not None  # ruff: ignore[assert]
        cancelling = caller.cancelling()
        task = asyncio.create_task(operation, name="stomp-write")
        self._writing.add(task)
        try:
            await task
        except asyncio.CancelledError as error:
            if self.ended.done() and caller.cancelling() == cancelling:
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
            await self._transport_write(self.transport.write_frame(frame))
            self._last_sent = time.monotonic()

    def command(
        self,
        frame: AnyClientFrame,
        confirmation: Confirmation = DEFAULT_CONFIRMATION,
        rejected: Callable[[ReceiptRejectedError], None] = ignore_rejection,
    ) -> Command:
        return Command(self, frame, confirmation, rejected)

    async def write(
        self, frame: AnyClientFrame, confirmation: Confirmation = DEFAULT_CONFIRMATION
    ) -> ReceiptFrame | None:
        command = self.command(frame, confirmation)
        try:
            await command.submit()
            return await command.complete()
        finally:
            command.close()

    async def _monitor(self) -> None:
        while not self.ended.done():
            await asyncio.sleep(self.heartbeat.want_to_receive_interval_ms / 1000)
            if not self.is_alive():
                self.fail(ConnectionLostError(reason="negotiated receive heartbeat expired"))

    async def _send_heartbeats(self) -> None:
        interval = self.heartbeat.will_send_interval_ms / 1000
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            while not self.ended.done():
                await asyncio.sleep(interval)
                async with self._writes:
                    if self.ended.done():
                        return
                    if time.monotonic() - self._last_sent >= interval:
                        await self._transport_write(self.transport.send_heartbeat())
                        self._last_sent = time.monotonic()
        except Exception as error:  # ruff: ignore[blind-except]
            self.fail(error)

    async def _monitor_idle(self) -> None:
        timeout = self._settings.idle_timeout
        while not self.ended.done():
            await asyncio.sleep(max(0, timeout - (time.monotonic() - self._last_message)))
            if time.monotonic() - self._last_message >= timeout:
                self.fail(ConnectionLostError(reason="no messages received within timeout"))

    async def close(self, *, graceful: bool = False) -> None:
        if not isinstance(self._close_state, Closing):
            self._close_state = Closing(asyncio.create_task(self._close(graceful=graceful), name="stomp-close"))
        await await_cleanup(self._close_state.task)

    async def _close(self, *, graceful: bool) -> None:
        try:
            if graceful and not self.ended.done():
                with suppress(ConnectionLostError, ReceiptRejectedError, ReceiptTimeoutError):
                    await self.write(DisconnectFrame(headers={}), Confirmed(self._settings.disconnect_timeout))
        finally:
            self.fail(ConnectionLostError(reason="session closed"))
            for task in self._tasks:
                task.cancel()
            try:
                await asyncio.gather(*self._tasks, *self._writing, return_exceptions=True)
                await self._frames.aclose()
            finally:
                await self.transport.close()
