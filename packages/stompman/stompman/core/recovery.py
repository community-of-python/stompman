"""Connection acquisition and restoration, with one lock for generation changes.

The lock covers transport submission and journal mutation. Receipt waits happen
outside it, so an unresponsive operation cannot serialize unrelated operations.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, Protocol, TypeVar

from ._tasks import Cancellation, await_cleanup
from .command import Command
from .config import DEFAULT_CONFIRMATION, Confirmation, Heartbeat, RuntimeConfig
from .connector import Connector, Unavailable
from .errors import (
    AnyConnectionIssue,
    ConnectionLostError,
    ConnectionLostOnLifespanEnter,
    FailedAllConnectAttemptsError,
    FailedAllWriteAttemptsError,
)
from .frames import AnyClientFrame, AnyServerFrame, ReceiptFrame
from .session import Session

T = TypeVar("T")


class Restorable(Protocol):
    @property
    def key(self) -> tuple[str, str]: ...
    async def restore(self, session: Session) -> None: ...
    def disconnected(self, session: Session) -> None: ...
    def retire(self) -> None: ...


@dataclass(frozen=True, slots=True)
class Disconnected:
    not_before: float


@dataclass(frozen=True, slots=True)
class Restoring:
    session: Session
    since: float


@dataclass(frozen=True, slots=True)
class Connected:
    session: Session
    since: float


@dataclass(frozen=True, slots=True)
class Failed:
    error: Exception


@dataclass(frozen=True, slots=True)
class Closed: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectionSnapshot:
    state: Literal["connected", "recovering", "failed"]
    generation: int
    heartbeat: Heartbeat
    failure: Exception | None
    pending_receipts: int
    writing: bool


class ConnectionSupervisor:
    def __init__(
        self,
        config: RuntimeConfig,
        connector: Connector,
        receive: Callable[[AnyServerFrame, Session], None],
        generation: int,
    ) -> None:
        self.config = config
        self.generation = generation
        self._connector = connector
        self._receive = receive
        self._state: Disconnected | Restoring | Connected | Failed | Closed = Disconnected(0)
        self._gate = asyncio.Lock()
        self._stopping = asyncio.Event()
        self._acquisitions: set[asyncio.Task[Session]] = set()
        self._resources: dict[tuple[str, str], Restorable] = {}

    def is_alive(self) -> bool:
        return isinstance(self._state, Connected) and self._state.session.is_alive()

    def snapshot(self) -> ConnectionSnapshot:
        state = self._state
        phase: Literal["connected", "recovering", "failed"] = "recovering"
        if isinstance(state, Failed):
            phase = "failed"
        elif isinstance(state, Connected) and not state.session.ended.done():
            phase = "connected"
        return ConnectionSnapshot(
            state=phase,
            generation=self.generation,
            heartbeat=state.session.heartbeat if isinstance(state, (Restoring, Connected)) else Heartbeat(0, 0),
            failure=state.error if isinstance(state, Failed) else None,
            pending_receipts=state.session.receipts.pending_count if isinstance(state, (Restoring, Connected)) else 0,
            writing=state.session.writing if isinstance(state, (Restoring, Connected)) else False,
        )

    def attach(self, resource: Restorable) -> None:
        if resource.key in self._resources:
            msg = f"{resource.key[0]} id is already active"
            raise ValueError(msg)
        self._resources[resource.key] = resource

    def detach(self, resource: Restorable) -> None:
        if self._resources.get(resource.key) is resource:
            self._resources.pop(resource.key)

    def rekey(self, resource: Restorable, previous_key: tuple[str, str]) -> None:
        existing = self._resources.get(resource.key)
        if existing is not None and existing is not resource:
            msg = f"{resource.key[0]} id is already active"
            raise ValueError(msg)
        if self._resources.get(previous_key) is resource:
            self._resources.pop(previous_key)
        self._resources[resource.key] = resource

    async def _discard(self) -> None:
        state = self._state
        if isinstance(state, (Restoring, Connected)):
            self._state = Disconnected(state.since + self.config.recovery.delay)
            for resource in tuple(self._resources.values()):
                resource.disconnected(state.session)
            await state.session.close()

    async def _acquire(self) -> Session:
        if self._stopping.is_set() or isinstance(self._state, Closed):
            msg = "runtime is not running"
            raise RuntimeError(msg)
        if isinstance(self._state, Failed):
            raise self._state.error
        if isinstance(self._state, Connected):
            if not self._state.session.ended.done():
                return self._state.session
            failure = self._state.session.ended.result()
            if not isinstance(failure, (ConnectionLostError, OSError)):
                raise failure
        await self._discard()
        if self._stopping.is_set():
            msg = "runtime closed during connection acquisition"
            raise RuntimeError(msg)
        task = asyncio.create_task(self._connect_ready(), name="stomp-acquisition")
        self._acquisitions.add(task)
        cancellation = Cancellation.capture()
        try:
            return await task
        except asyncio.CancelledError as error:
            if self._stopping.is_set() and not cancellation.requested:
                msg = "runtime closed during connection acquisition"
                raise RuntimeError(msg) from error
            raise
        finally:
            self._acquisitions.discard(task)

    async def _connect_ready(self) -> Session:
        issues: list[AnyConnectionIssue] = []
        for attempt in range(self.config.recovery.attempts):
            if isinstance(self._state, Disconnected):
                await asyncio.sleep(max(0, self._state.not_before - time.monotonic()))
            result = await self._connector.connect()
            if isinstance(result, Unavailable):
                issues.extend(result.issues)
            else:
                session = Session(result, self.config.connection, self.generation + 1, self._receive)
                restoring = Restoring(session, time.monotonic())
                self._state = restoring
                if await self._restore(session):
                    self.generation = session.generation
                    self._state = Connected(session, restoring.since)
                    return session
                issues.append(ConnectionLostOnLifespanEnter())
            if attempt + 1 < self.config.recovery.attempts:
                await asyncio.sleep(self.config.recovery.delay * (attempt + 1))
        raise FailedAllConnectAttemptsError(retry_attempts=self.config.recovery.attempts, issues=issues)

    async def _restore(self, session: Session) -> bool:
        try:
            for resource in tuple(self._resources.values()):
                await resource.restore(session)
            session.check()
        except ConnectionLostError:
            await self._discard()
            return False
        except BaseException:
            # A cancelled or failed restore cannot leave a partially ready generation.
            await await_cleanup(self._discard())
            raise
        return True

    async def write_current(self, frame: AnyClientFrame, confirmation: Confirmation) -> bool:
        """Finish a command on the current session without opening a replacement."""
        async with self._gate:
            state = self._state
            if self._stopping.is_set() or not isinstance(state, Connected) or state.session.ended.done():
                return False
            command = await state.session.submit(frame, confirmation)
        await command.complete()
        return True

    async def run(self, operation: Callable[[Session], Awaitable[T]], *, attempts: int = 1) -> T:
        """Run submission and its atomic state update in the current generation."""
        async with self._gate:
            for attempt in range(attempts):
                session = await self._acquire()
                try:
                    return await operation(session)
                except ConnectionLostError:
                    await self._discard()
                    if attempts == 1:
                        raise
                    if attempt + 1 == attempts:
                        raise FailedAllWriteAttemptsError(retry_attempts=attempts) from None
        msg = "attempt count must be positive"
        raise ValueError(msg)

    async def write(
        self, frame: AnyClientFrame, confirmation: Confirmation = DEFAULT_CONFIRMATION
    ) -> ReceiptFrame | None:
        async def submit(session: Session) -> Command:
            return await session.submit(frame, confirmation)

        command = await self.run(submit, attempts=confirmation.attempts)
        return await command.complete()

    async def start(self) -> None:
        async with self._gate:
            await self._acquire()

    async def reconnect(self) -> None:
        async with self._gate:
            await self._discard()
            await self._acquire()

    async def supervise(self) -> None:
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            while True:
                if isinstance(self._state, Connected):
                    await asyncio.shield(self._state.session.ended)
                try:
                    await self.start()
                except FailedAllConnectAttemptsError:
                    if not self.config.recovery.keep_trying:
                        raise
                    await asyncio.sleep(self.config.recovery.delay)
        except Exception as error:
            await self._discard()
            self._state = Failed(error)
            raise

    async def close(self, *, graceful: bool) -> None:
        # Acquisition is owned work: shutdown can cancel it without cancelling
        # an application task that happened to request a reconnect or publication.
        self._stopping.set()
        if isinstance(self._state, (Restoring, Connected)) and self._state.session.writing:
            self._state.session.fail(ConnectionLostError(reason="runtime closed during transport write"))
        for task in self._acquisitions:
            task.cancel()
        await asyncio.gather(*self._acquisitions, return_exceptions=True)
        async with self._gate:
            try:
                if isinstance(self._state, (Restoring, Connected)):
                    await self._state.session.close(graceful=graceful)
            finally:
                for resource in tuple(self._resources.values()):
                    resource.retire()
                self._resources.clear()
                self._state = Closed()
