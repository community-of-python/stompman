"""The legacy manager interface delegates execution to the independent runtime."""

import asyncio
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from ssl import SSLContext
from types import TracebackType
from typing import TYPE_CHECKING, Literal, Self, cast

from ._compat import LegacyOptions
from ._legacy_errors import translate_connection_errors
from ._manager_transport import LifespanTransport, ObservedTransport
from .config import ConnectionParameters, Heartbeat
from .connection import AbstractConnection
from .core import Runtime, Unconfirmed
from .core.transaction import Transaction as CoreTransaction
from .errors import (
    ConnectionLostError,
    ConnectionLostOnLifespanEnter,
    FailedAllConnectAttemptsError,
    FailedAllWriteAttemptsError,
    StompProtocolConnectionIssue,
)
from .frames import (
    AbortFrame,
    AnyClientFrame,
    AnyServerFrame,
    BeginFrame,
    CommitFrame,
    SendFrame,
)

if TYPE_CHECKING:
    from ._compat import LegacyTransport
    from .connection_lifespan import AbstractConnectionLifespan, ConnectionLifespanFactory, EstablishedConnectionResult
    from .core.transport import Transport

ConnectionRestoration = Callable[[], Awaitable[None]]


async def _restore(transport: ObservedTransport, restoration: ConnectionRestoration) -> None:
    try:
        await restoration()
    except ConnectionLostError as error:
        # The reader invalidates the session. Closing it here would wait for
        # this same restoration task during native session cleanup.
        transport.fail(error)


@dataclass(frozen=True, kw_only=True, slots=True)
class ActiveConnectionState:
    connection: AbstractConnection
    lifespan: "AbstractConnectionLifespan"
    server_heartbeat: Heartbeat
    connected_at: float
    restoration_task: asyncio.Task[None] | None = field(default=None, repr=False, compare=False)

    def is_alive(self, check_server_alive_interval_factor: int) -> bool:
        receive = self.server_heartbeat.will_send_interval_ms / 1000
        if not receive:
            return True
        last_read = self.connection.last_read_time
        return (
            time.time() - (self.connected_at if last_read is None else last_read)
            < receive * check_server_alive_interval_factor
        )


@dataclass(kw_only=True, slots=True)
class ConnectionManager:
    servers: list[ConnectionParameters]
    lifespan_factory: "ConnectionLifespanFactory"
    connection_class: type[AbstractConnection]
    connect_retry_attempts: int
    connect_retry_interval: int
    connect_timeout: int
    ssl: Literal[True] | SSLContext | None
    read_max_chunk_size: int
    write_retry_attempts: int
    check_server_alive_interval_factor: int
    no_message_restart_interval: timedelta | None
    keep_alive_on_connection_failure: bool = False
    on_connection_lost: Callable[[AbstractConnection], None] | None = None
    restore_connection: Callable[[AbstractConnection], ConnectionRestoration | None] | None = None
    _runtime: Runtime | None = field(default=None, init=False, repr=False, compare=False)
    _runtime_factory: Callable[[], Runtime] | None = field(default=None, init=False, repr=False, compare=False)
    _transactions: dict[str, CoreTransaction] = field(default_factory=dict, init=False, repr=False, compare=False)
    _replay_sources: dict[str, Callable[[], tuple[SendFrame, ...]]] = field(
        default_factory=dict, init=False, repr=False, compare=False
    )
    _active_connection_state: ActiveConnectionState | None = field(default=None, init=False, repr=False, compare=False)
    _transport: "ObservedTransport | None" = field(default=None, init=False, repr=False, compare=False)
    _connection_available: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False, compare=False)
    _frames: asyncio.Queue[tuple[AnyServerFrame, int] | ConnectionLostError | None] | None = field(
        default=None, init=False, repr=False, compare=False
    )

    def bind(self, runtime_factory: Callable[[], Runtime]) -> None:
        self._runtime_factory = runtime_factory

    @contextmanager
    def transaction_replay(self, transaction_id: str, source: Callable[[], tuple[SendFrame, ...]]) -> Iterator[None]:
        self._replay_sources[transaction_id] = source
        try:
            yield
        finally:
            self._replay_sources.pop(transaction_id, None)

    @property
    def runtime(self) -> Runtime:
        if self._runtime_factory is not None:
            return self._runtime_factory()
        if self._runtime is None:
            options = self.lifespan_factory.keywords if isinstance(self.lifespan_factory, partial) else {}
            self._runtime = LegacyOptions(
                self.servers,
                connection_class=self.connection_class,
                ssl=self.ssl,
                connect_retry_attempts=self.connect_retry_attempts,
                connect_retry_interval=self.connect_retry_interval,
                connect_timeout=self.connect_timeout,
                read_max_chunk_size=self.read_max_chunk_size,
                write_retry_attempts=self.write_retry_attempts,
                check_server_alive_interval_factor=self.check_server_alive_interval_factor,
                no_message_restart_interval=self.no_message_restart_interval,
                keep_alive_on_connection_failure=self.keep_alive_on_connection_failure,
                heartbeat=options.get("client_heartbeat", Heartbeat(1000, 1000)),
                connection_confirmation_timeout=options.get("connection_confirmation_timeout", 2),
                # The explicitly supplied lifespan owns graceful disconnection.
                disconnect_confirmation_timeout=0,
            ).to_runtime(wrap_transport=self._enter_lifespan)
        return self._runtime

    @property
    def _reconnection_count(self) -> int:
        return max(0, self.runtime.status.generation - 1)

    async def __aenter__(self) -> Self:
        with translate_connection_errors(self.servers, timeout=self.connect_timeout):
            await self.runtime.start()
            try:
                await self._get_restored_connection_state(is_initial_call=True)
            except BaseException as error:
                await self.runtime.close(type(error), error, error.__traceback__)
                raise
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        try:
            with translate_connection_errors(self.servers, timeout=self.connect_timeout):
                await self.runtime.close(exc_type, exc_value, traceback)
        finally:
            self._transactions.clear()
            if self._frames is not None:
                self._frames.put_nowait(None)

    async def write_frame_reconnecting(self, frame: AnyClientFrame) -> None:
        if self.write_retry_attempts <= 0:
            raise FailedAllWriteAttemptsError(retry_attempts=self.write_retry_attempts)
        with translate_connection_errors(self.servers, timeout=self.connect_timeout):
            try:
                await self._write(frame)
            except ConnectionLostError as error:
                raise FailedAllWriteAttemptsError(retry_attempts=self.write_retry_attempts) from error

    async def _write(self, frame: AnyClientFrame) -> None:
        if self.restore_connection is not None:
            await self._get_restored_connection_state()
        confirmation = Unconfirmed(self.write_retry_attempts)
        if isinstance(frame, BeginFrame):
            transaction = self.runtime.begin(
                transaction_id=frame.headers["transaction"],
                confirmation=confirmation,
                commit_confirmation=Unconfirmed(),
                replay_source=self._replay_sources.get(frame.headers["transaction"]),
            )
            await transaction.__aenter__()
            self._transactions[transaction.id] = transaction
        elif (
            isinstance(frame, SendFrame)
            and (transaction_id := frame.headers.get("transaction")) is not None
            and transaction_id in self._transactions
        ):
            await self._transactions[transaction_id].send(
                frame.body,
                frame.headers["destination"],
                content_type=frame.headers.get("content-type"),
                add_content_length="content-length" in frame.headers,
                headers=cast("dict[str, str]", dict(frame.headers)),
            )
        else:
            await self.runtime.write_frame(frame, confirmation=confirmation)

    async def maybe_write_frame(self, frame: AnyClientFrame) -> bool:
        with translate_connection_errors(self.servers, timeout=self.connect_timeout):
            if isinstance(frame, (CommitFrame, AbortFrame)) and frame.headers["transaction"] in self._transactions:
                transaction = self._transactions.pop(frame.headers["transaction"])
                if isinstance(frame, CommitFrame):
                    await transaction.commit()
                else:
                    await transaction.abort()
                return True
            if not self.runtime.is_alive():
                return False
            with suppress(ConnectionLostError):
                return await self.runtime.submit_if_connected(frame, Unconfirmed())
            return False

    async def read_frames_reconnecting(self) -> AsyncGenerator[tuple[AnyServerFrame, int], None]:
        if self._frames is not None:
            msg = "a frame reader is already active"
            raise RuntimeError(msg)
        self._frames = asyncio.Queue()
        try:
            while True:
                with translate_connection_errors(self.servers, timeout=self.connect_timeout):
                    await self.runtime.ensure_connected()
                item = await self._frames.get()
                if item is None:
                    return
                if not isinstance(item, ConnectionLostError):
                    yield item
        finally:
            self._frames = None

    async def write_heartbeat_reconnecting(self) -> None:
        for _ in range(self.write_retry_attempts):
            await self._get_active_connection_state()
            transport = self._transport
            if transport is None:
                continue
            try:
                await transport.send_heartbeat()
            except ConnectionLostError:
                with translate_connection_errors(self.servers, timeout=self.connect_timeout):
                    await self.runtime.reconnect()
            else:
                return
        raise FailedAllWriteAttemptsError(retry_attempts=self.write_retry_attempts)

    async def observe_transport(self, transport: "LegacyTransport", server: ConnectionParameters) -> "Transport":
        lifespan = self.lifespan_factory(
            connection=transport.connection,
            connection_parameters=server,
            set_heartbeat_interval=lambda _heartbeat: None,
        )
        return ObservedTransport(transport, transport, lifespan, self)

    async def _enter_lifespan(self, transport: "LegacyTransport", server: ConnectionParameters) -> "Transport":
        lifespan = self.lifespan_factory(
            connection=transport.connection,
            connection_parameters=server,
            set_heartbeat_interval=lambda _heartbeat: None,
        )
        result: EstablishedConnectionResult | StompProtocolConnectionIssue | ConnectionLostError
        try:
            result = await lifespan.enter()
        except ConnectionLostError as error:
            result = error
        protocol = self.runtime.config.connection.protocol_version
        adapted = LifespanTransport(transport, lifespan, result, protocol)
        return ObservedTransport(adapted, transport, lifespan, self)

    def _connection_opened(self, transport: "ObservedTransport", heartbeat: Heartbeat) -> None:
        self._transport = transport
        restoration = self.restore_connection(transport.raw.connection) if self.restore_connection is not None else None
        self._active_connection_state = ActiveConnectionState(
            connection=transport.raw.connection,
            lifespan=transport.lifespan,
            server_heartbeat=heartbeat,
            connected_at=time.time(),
            restoration_task=asyncio.create_task(_restore(transport, restoration)) if restoration is not None else None,
        )
        self._connection_available.set()

    def _frame_received(self, transport: "ObservedTransport", frame: AnyServerFrame) -> None:
        if self._frames is not None and self._transport is transport:
            self._frames.put_nowait((frame, self._reconnection_count))

    async def _connection_closed(self, transport: "ObservedTransport", *, graceful: bool) -> None:
        if self._transport is not transport:
            return
        state = self._active_connection_state
        self._transport = None
        self._active_connection_state = None
        self._connection_available.clear()
        if (
            state is not None
            and state.restoration_task is not None
            and state.restoration_task is not asyncio.current_task()
        ):
            state.restoration_task.cancel()
            await asyncio.gather(state.restoration_task, return_exceptions=True)
        if not graceful:
            if self.on_connection_lost is not None:
                self.on_connection_lost(transport.raw.connection)
            if self._frames is not None:
                self._frames.put_nowait(ConnectionLostError(reason="observed connection ended"))

    async def _get_active_connection_state(self, *, is_initial_call: bool = False) -> ActiveConnectionState:
        del is_initial_call
        while True:
            with translate_connection_errors(self.servers, timeout=self.connect_timeout):
                await self.runtime.ensure_connected()
            await self._connection_available.wait()
            if self._active_connection_state is not None:
                return self._active_connection_state

    async def _get_restored_connection_state(self, *, is_initial_call: bool = False) -> ActiveConnectionState:
        for _attempt in range(self.connect_retry_attempts):
            state = await self._get_active_connection_state(is_initial_call=is_initial_call)
            if state.restoration_task is not None:
                # A cancelled caller must not cancel shared restoration. A task
                # cancelled by session cleanup belongs to a discarded state.
                await asyncio.wait((state.restoration_task,))
            if self._active_connection_state is not state:
                continue
            if state.restoration_task is not None:
                state.restoration_task.result()
            transport = self._transport
            if transport is not None and transport.failed:
                await transport.wait_closed()
                continue
            return state
        raise FailedAllConnectAttemptsError(
            retry_attempts=self.connect_retry_attempts,
            issues=[ConnectionLostOnLifespanEnter() for _attempt in range(self.connect_retry_attempts)],
        )

    async def _discard_failed_connection_state(
        self, connection_state: ActiveConnectionState, error_reason: ConnectionLostError
    ) -> None:
        del error_reason
        if self._active_connection_state is connection_state:
            with translate_connection_errors(self.servers, timeout=self.connect_timeout):
                await self.runtime.reconnect()
