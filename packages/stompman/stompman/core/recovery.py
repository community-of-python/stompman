"""Connection acquisition and restoration, with one lock for generation changes.

The lock covers transport submission and journal mutation. Receipt waits happen
outside it, so an unresponsive operation cannot serialize unrelated operations.
"""

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, TypeVar

from ._tasks import await_cleanup
from .config import DEFAULT_CONFIRMATION, Confirmation, Heartbeat, RuntimeConfig, Server, Unconfirmed
from .errors import (
    AllServersUnavailable,
    AnyConnectionIssue,
    ConnectionLostError,
    ConnectionLostOnLifespanEnter,
    FailedAllConnectAttemptsError,
    FailedAllWriteAttemptsError,
)
from .frames import AnyClientFrame, AnyServerFrame, ReceiptFrame
from .session import Command, HandshakeFailedError, Session
from .transport import TransportFactory

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
class Connected:
    session: Session
    since: float


@dataclass(frozen=True, slots=True)
class Failed:
    error: Exception


@dataclass(frozen=True, slots=True)
class Closed: ...


class ConnectionSupervisor:
    def __init__(
        self,
        config: RuntimeConfig,
        factory: TransportFactory,
        receive: Callable[[AnyServerFrame, Session], None],
        generation: int,
    ) -> None:
        self.config = config
        self.generation = generation
        self._factory = factory
        self._receive = receive
        self._state: Disconnected | Connected | Failed | Closed = Disconnected(0)
        self._gate = asyncio.Lock()
        self._stopping = asyncio.Event()
        self._acquisitions: set[asyncio.Task[Session]] = set()
        self._resources: dict[tuple[str, str], Restorable] = {}

    @property
    def state(self) -> Disconnected | Connected | Failed | Closed:
        return self._state

    @property
    def heartbeat(self) -> Heartbeat:
        return self._state.session.heartbeat if isinstance(self._state, Connected) else Heartbeat(0, 0)

    def is_alive(self) -> bool:
        return isinstance(self._state, Connected) and self._state.session.is_alive()

    def attach(self, resource: Restorable) -> None:
        if resource.key in self._resources:
            msg = f"{resource.key[0]} id is already active"
            raise ValueError(msg)
        self._resources[resource.key] = resource

    def detach(self, resource: Restorable) -> None:
        if self._resources.get(resource.key) is resource:
            self._resources.pop(resource.key)

    async def _candidate(self, server: Server) -> Session | AnyConnectionIssue:
        try:
            transport = await self._factory(server, self.config.connection)
        except (OSError, ConnectionLostError):
            return AllServersUnavailable(servers=[server], timeout=self.config.connection.timeout)
        try:
            return await Session.open(transport, server, self.config.connection, self.generation + 1)
        except HandshakeFailedError as error:
            return error.issue
        except (OSError, ConnectionLostError, ValueError):
            return ConnectionLostOnLifespanEnter()

    async def _race(self) -> Session | list[AnyConnectionIssue]:
        tasks = [asyncio.create_task(self._candidate(server)) for server in self.config.servers]
        issues: list[AnyConnectionIssue] = []

        async def cleanup(keep: Session | list[AnyConnectionIssue]) -> None:
            for task in tasks:
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(*(item.close() for item in results if isinstance(item, Session) and item is not keep))

        try:
            for completed in asyncio.as_completed(tasks):
                result = await completed
                if isinstance(result, Session):
                    await await_cleanup(asyncio.create_task(cleanup(result)))
                    return result
                issues.append(result)
        except BaseException:
            await await_cleanup(asyncio.create_task(cleanup(issues)))
            raise
        return issues

    async def _discard(self) -> None:
        state = self._state
        if isinstance(state, Connected):
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
        caller = asyncio.current_task()
        assert caller is not None  # ruff: ignore[assert]
        cancelling = caller.cancelling()
        try:
            return await task
        except asyncio.CancelledError as error:
            if self._stopping.is_set() and caller.cancelling() == cancelling:
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
            result = await self._race()
            if isinstance(result, list):
                issues.extend(result)
            else:
                self.generation = result.generation
                self._state = Connected(result, time.monotonic())
                result.start(self._receive)
                if await self._restore(result):
                    return result
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
            await await_cleanup(asyncio.create_task(self._discard()))
            raise
        return True

    async def submit_current(self, frame: AnyClientFrame, confirmation: Confirmation) -> Command | None:
        """Submit cleanup after restoration, without opening a replacement session."""
        async with self._gate:
            state = self._state
            if self._stopping.is_set() or not isinstance(state, Connected) or state.session.ended.done():
                return None
            command = state.session.command(frame, confirmation)
            try:
                await command.submit()
            except BaseException:
                command.close()
                raise
            return command

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
            command = session.command(frame, confirmation)
            try:
                await command.submit()
            except BaseException:
                command.close()
                raise
            return command

        attempts = confirmation.attempts if isinstance(confirmation, Unconfirmed) else 1
        command = await self.run(submit, attempts=attempts)
        try:
            return await command.complete()
        finally:
            command.close()

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
        if isinstance(self._state, Connected) and self._state.session.writing:
            self._state.session.fail(ConnectionLostError(reason="runtime closed during transport write"))
        for task in self._acquisitions:
            task.cancel()
        await asyncio.gather(*self._acquisitions, return_exceptions=True)
        async with self._gate:
            try:
                if isinstance(self._state, Connected):
                    await self._state.session.close(graceful=graceful)
            finally:
                for resource in tuple(self._resources.values()):
                    resource.retire()
                self._resources.clear()
                self._state = Closed()
