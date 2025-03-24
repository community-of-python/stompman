import asyncio
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from ssl import SSLContext
from types import TracebackType
from typing import TYPE_CHECKING, Literal, Self

from stompman.config import ConnectionParameters
from stompman.connection import AbstractConnection
from stompman.errors import (
    AllServersUnavailable,
    AnyConnectionIssue,
    ConnectionLost,
    ConnectionLostError,
    FailedAllConnectAttemptsError,
    FailedAllWriteAttemptsError,
)
from stompman.frames import AnyClientFrame, AnyServerFrame

if TYPE_CHECKING:
    from stompman.connection_lifespan import AbstractConnectionLifespan, ConnectionLifespanFactory


@dataclass(frozen=True, kw_only=True, slots=True)
class ActiveConnectionState:
    connection: AbstractConnection
    lifespan: "AbstractConnectionLifespan"


@dataclass(kw_only=True, slots=True)
class ConnectionManager:
    servers: list[ConnectionParameters]
    lifespan_factory: "ConnectionLifespanFactory"
    connection_class: type[AbstractConnection]
    connect_retry_attempts: int
    connect_retry_interval: int
    connect_timeout: int
    ssl: Literal[True] | SSLContext | None
    read_timeout: int
    read_max_chunk_size: int
    write_retry_attempts: int
    check_server_alive_interval_factor: int

    _active_connection_state: ActiveConnectionState | None = field(default=None, init=False)
    _reconnect_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def __aenter__(self) -> Self:
        self._active_connection_state = await self._get_active_connection_state()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        if not self._active_connection_state:
            return
        try:
            await self._active_connection_state.lifespan.exit()
        except ConnectionLostError:
            return
        await self._active_connection_state.connection.close()

    async def _create_connection_to_one_server(self, server: ConnectionParameters) -> ActiveConnectionState | None:
        if connection := await self.connection_class.connect(
            host=server.host,
            port=server.port,
            timeout=self.connect_timeout,
            read_max_chunk_size=self.read_max_chunk_size,
            read_timeout=self.read_timeout,
            ssl=self.ssl,
        ):
            return ActiveConnectionState(
                connection=connection,
                lifespan=self.lifespan_factory(connection=connection, connection_parameters=server),
            )
        return None

    async def _create_connection_to_any_server(self) -> ActiveConnectionState | None:
        for maybe_connection_future in asyncio.as_completed(
            [self._create_connection_to_one_server(server) for server in self.servers]
        ):
            if connection_state := await maybe_connection_future:
                return connection_state
        return None

    async def _connect_to_any_server(self) -> ActiveConnectionState | AnyConnectionIssue:
        if not (active_connection_state := await self._create_connection_to_any_server()):
            return AllServersUnavailable(servers=self.servers, timeout=self.connect_timeout)

        try:
            if connection_issue := await active_connection_state.lifespan.enter():
                return connection_issue
        except ConnectionLostError:
            return ConnectionLost()

        return active_connection_state

    async def _get_active_connection_state(self) -> ActiveConnectionState:
        if self._active_connection_state:
            return self._active_connection_state

        connection_issues: list[AnyConnectionIssue] = []

        async with self._reconnect_lock:
            if self._active_connection_state:
                return self._active_connection_state

            for attempt in range(self.connect_retry_attempts):
                connection_result = await self._connect_to_any_server()

                if isinstance(connection_result, ActiveConnectionState):
                    self._active_connection_state = connection_result
                    return connection_result

                connection_issues.append(connection_result)
                await asyncio.sleep(self.connect_retry_interval * (attempt + 1))

        raise FailedAllConnectAttemptsError(retry_attempts=self.connect_retry_attempts, issues=connection_issues)

    def _clear_active_connection_state(self) -> None:
        self._active_connection_state = None

    def is_alive(self) -> bool:
        if not self._connection_manager._active_connection_state:
            return False
        if not (last_read_time_ms := self._connection_manager._active_connection_state.connection.last_read_time_ms):
            return True
        if (time.time() - last_read_time_ms) > server_heartbeat_interval_seconds:
            ...
        return None

    async def write_heartbeat_reconnecting(self) -> None:
        for _ in range(self.write_retry_attempts):
            connection_state = await self._get_active_connection_state()
            try:
                return connection_state.connection.write_heartbeat()
            except ConnectionLostError:
                self._clear_active_connection_state()

        raise FailedAllWriteAttemptsError(retry_attempts=self.write_retry_attempts)

    async def write_frame_reconnecting(self, frame: AnyClientFrame) -> None:
        for _ in range(self.write_retry_attempts):
            connection_state = await self._get_active_connection_state()
            try:
                return await connection_state.connection.write_frame(frame)
            except ConnectionLostError:
                self._clear_active_connection_state()

        raise FailedAllWriteAttemptsError(retry_attempts=self.write_retry_attempts)

    async def read_frames_reconnecting(self) -> AsyncGenerator[AnyServerFrame, None]:
        while True:
            connection_state = await self._get_active_connection_state()
            try:
                async for frame in connection_state.connection.read_frames():
                    yield frame
            except ConnectionLostError:
                self._clear_active_connection_state()

    async def maybe_write_frame(self, frame: AnyClientFrame) -> bool:
        if not self._active_connection_state:
            return False
        try:
            await self._active_connection_state.connection.write_frame(frame)
        except ConnectionLostError:
            self._clear_active_connection_state()
            return False
        return True
