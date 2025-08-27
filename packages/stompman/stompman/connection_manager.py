import asyncio
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from ssl import SSLContext
from types import TracebackType
from typing import TYPE_CHECKING, Literal, Self

from stompman.config import ConnectionParameters, Heartbeat
from stompman.connection import AbstractConnection
from stompman.errors import (
    AllServersUnavailable,
    AnyConnectionIssue,
    ConnectionLostError,
    ConnectionLostOnLifespanEnter,
    FailedAllConnectAttemptsError,
    FailedAllWriteAttemptsError,
)
from stompman.frames import AnyClientFrame, AnyServerFrame
from stompman.logger import LOGGER

if TYPE_CHECKING:
    from stompman.connection_lifespan import AbstractConnectionLifespan, ConnectionLifespanFactory


@dataclass(frozen=True, kw_only=True, slots=True)
class ActiveConnectionState:
    connection: AbstractConnection
    lifespan: "AbstractConnectionLifespan"
    server_heartbeat: Heartbeat

    def is_alive(self, check_server_alive_interval_factor: int) -> bool:
        if not (last_read_time := self.connection.last_read_time):
            return True

        return (self.server_heartbeat.will_send_interval_ms / 1000 * check_server_alive_interval_factor) > (
            time.time() - last_read_time
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

    _active_connection_state: ActiveConnectionState | None = field(default=None, init=False)
    _reconnect_lock: asyncio.Lock = field(init=False, default_factory=asyncio.Lock)
    _task_group: asyncio.TaskGroup = field(init=False, default_factory=asyncio.TaskGroup)
    _send_heartbeat_task: asyncio.Task[None] = field(init=False, repr=False)
    _check_server_heartbeat_task: asyncio.Task[None] = field(init=False, repr=False)

    async def __aenter__(self) -> Self:
        await self._task_group.__aenter__()
        self._send_heartbeat_task = self._task_group.create_task(asyncio.sleep(0))
        self._check_server_heartbeat_task = self._task_group.create_task(asyncio.sleep(0))
        self._active_connection_state = await self._get_active_connection_state(is_initial_call=True)
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self._send_heartbeat_task.cancel()
        self._check_server_heartbeat_task.cancel()
        await asyncio.wait([self._send_heartbeat_task, self._check_server_heartbeat_task])
        await self._task_group.__aexit__(exc_type, exc_value, traceback)

        if not self._active_connection_state:
            return
        try:
            await self._active_connection_state.lifespan.exit()
        except ConnectionLostError:
            return
        await self._active_connection_state.connection.close()

    def _restart_heartbeat_tasks(self, server_heartbeat: Heartbeat) -> None:
        self._send_heartbeat_task.cancel()
        self._check_server_heartbeat_task.cancel()
        self._send_heartbeat_task = self._task_group.create_task(
            self._send_heartbeats_forever(server_heartbeat.want_to_receive_interval_ms)
        )
        self._check_server_heartbeat_task = self._task_group.create_task(
            self._check_server_heartbeat_forever(server_heartbeat.will_send_interval_ms)
        )

    async def _send_heartbeats_forever(self, send_heartbeat_interval_ms: int) -> None:
        send_heartbeat_interval_seconds = send_heartbeat_interval_ms / 1000
        while True:
            await self.write_heartbeat_reconnecting()
            await asyncio.sleep(send_heartbeat_interval_seconds)

    async def _check_server_heartbeat_forever(self, receive_heartbeat_interval_ms: int) -> None:
        receive_heartbeat_interval_seconds = receive_heartbeat_interval_ms / 1000
        while True:
            await asyncio.sleep(receive_heartbeat_interval_seconds * self.check_server_alive_interval_factor)
            if not self._active_connection_state:
                continue
            if not self._active_connection_state.is_alive(self.check_server_alive_interval_factor):
                self._clear_active_connection_state(ConnectionLostError(reason="server heartbeat timeout"))

    async def _create_connection_to_one_server(
        self, server: ConnectionParameters
    ) -> tuple[AbstractConnection, ConnectionParameters] | None:
        if connection := await self.connection_class.connect(
            host=server.host,
            port=server.port,
            timeout=self.connect_timeout,
            read_max_chunk_size=self.read_max_chunk_size,
            ssl=self.ssl,
        ):
            return (connection, server)
        return None

    async def _create_connection_to_any_server(self) -> tuple[AbstractConnection, ConnectionParameters] | None:
        for maybe_connection_future in asyncio.as_completed(
            [self._create_connection_to_one_server(server) for server in self.servers]
        ):
            if connection_and_server := await maybe_connection_future:
                return connection_and_server
        return None

    async def _connect_to_any_server(self) -> ActiveConnectionState | AnyConnectionIssue:
        from stompman.connection_lifespan import EstablishedConnectionResult  # noqa: PLC0415

        if not (connection_and_server := await self._create_connection_to_any_server()):
            return AllServersUnavailable(servers=self.servers, timeout=self.connect_timeout)
        connection, connection_parameters = connection_and_server
        lifespan = self.lifespan_factory(
            connection=connection,
            connection_parameters=connection_parameters,
            set_heartbeat_interval=self._restart_heartbeat_tasks,
        )

        try:
            connection_result = await lifespan.enter()
        except ConnectionLostError:
            return ConnectionLostOnLifespanEnter()

        return (
            ActiveConnectionState(
                connection=connection, lifespan=lifespan, server_heartbeat=connection_result.server_heartbeat
            )
            if isinstance(connection_result, EstablishedConnectionResult)
            else connection_result
        )

    async def _get_active_connection_state(self, *, is_initial_call: bool = False) -> ActiveConnectionState:
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
                    if not is_initial_call:
                        LOGGER.warning(
                            "reconnected after connection failure. connection_parameters: %s",
                            connection_result.lifespan.connection_parameters,
                        )
                    return connection_result

                connection_issues.append(connection_result)
                await asyncio.sleep(self.connect_retry_interval * (attempt + 1))

        raise FailedAllConnectAttemptsError(retry_attempts=self.connect_retry_attempts, issues=connection_issues)

    def _clear_active_connection_state(self, error_reason: ConnectionLostError) -> None:
        if not self._active_connection_state:
            return
        LOGGER.warning(
            "connection lost. reason: %s, connection_parameters: %s",
            error_reason.reason,
            self._active_connection_state.lifespan.connection_parameters,
        )
        self._active_connection_state = None

    async def write_heartbeat_reconnecting(self) -> None:
        for _ in range(self.write_retry_attempts):
            connection_state = await self._get_active_connection_state()
            try:
                return connection_state.connection.write_heartbeat()
            except ConnectionLostError as error:
                self._clear_active_connection_state(error)

        raise FailedAllWriteAttemptsError(retry_attempts=self.write_retry_attempts)

    async def write_frame_reconnecting(self, frame: AnyClientFrame) -> None:
        for _ in range(self.write_retry_attempts):
            connection_state = await self._get_active_connection_state()
            try:
                return await connection_state.connection.write_frame(frame)
            except ConnectionLostError as error:
                self._clear_active_connection_state(error)

        raise FailedAllWriteAttemptsError(retry_attempts=self.write_retry_attempts)

    async def read_frames_reconnecting(self) -> AsyncGenerator[AnyServerFrame, None]:
        while True:
            connection_state = await self._get_active_connection_state()
            try:
                async for frame in connection_state.connection.read_frames():
                    yield frame
            except ConnectionLostError as error:
                self._clear_active_connection_state(error)

    async def maybe_write_frame(self, frame: AnyClientFrame) -> bool:
        if not self._active_connection_state:
            return False
        try:
            await self._active_connection_state.connection.write_frame(frame)
        except ConnectionLostError as error:
            self._clear_active_connection_state(error)
            return False
        return True
