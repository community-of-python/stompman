"""Translate legacy defaults and transport conventions at the facade seam."""

import math
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from ssl import SSLContext
from typing import Any, ClassVar, Literal, Protocol, runtime_checkable

from ._legacy_protocol import LegacyProtocol
from .config import ConnectionParameters, Heartbeat
from .connection import AbstractConnection, Connection
from .core.config import (
    Confirmation,
    Confirmed,
    ConnectionSettings,
    DeliveryLimits,
    Paused,
    RecoveryPolicy,
    RuntimeConfig,
    Server,
    Unbounded,
    Unconfirmed,
)
from .core.errors import ConnectionLostError
from .core.frames import AnyClientFrame, AnyServerFrame, ConnectFrame, ErrorFrame
from .core.handshake import HandshakeFailedError
from .core.runtime import Runtime, log_error_frame
from .core.transport import Transport
from .errors import AllServersUnavailable, ConnectionConfirmationTimeout, FailedAllConnectAttemptsError


@runtime_checkable
class AsyncHeartbeat(Protocol):
    async def send_heartbeat(self) -> None: ...


class LegacyTransport:
    def __init__(self, connection: AbstractConnection, *, handshake_timeout: int = 2) -> None:
        self.connection = connection
        self._handshake_timeout = handshake_timeout
        self._opened_at = time.monotonic()

    @property
    def last_received_at(self) -> float:
        last_read = self.connection.last_read_time
        return self._opened_at if last_read is None else time.monotonic() - max(0, time.time() - last_read)

    async def write_frame(self, frame: AnyClientFrame) -> None:
        await self.connection.write_frame(frame)
        if isinstance(frame, ConnectFrame) and self._handshake_timeout <= 0:
            raise HandshakeFailedError(issue=ConnectionConfirmationTimeout(timeout=self._handshake_timeout, frames=[]))

    async def send_heartbeat(self) -> None:
        if isinstance(self.connection, AsyncHeartbeat):
            await self.connection.send_heartbeat()
        else:
            self.connection.write_heartbeat()

    def read_frames(self) -> AsyncGenerator[AnyServerFrame, None]:
        return self.connection.read_frames()

    async def close(self) -> None:
        await self.connection.close()


@dataclass(frozen=True, slots=True)
class LegacyTransportFactory:
    connection_class: type[AbstractConnection]
    paths: dict[int, str | None]
    timeout: int
    handshake_timeout: int = 2

    async def __call__(self, server: Server, settings: ConnectionSettings) -> LegacyTransport:
        connection = await self.connection_class.connect(
            host=server.host,
            port=server.port,
            timeout=self.timeout,
            read_max_chunk_size=settings.read_chunk_size,
            ssl=settings.tls or None,
            ws_uri_path=self.paths.get(id(server)),
        )
        if connection is None:
            raise ConnectionLostError(reason="transport could not connect")
        return LegacyTransport(connection, handshake_timeout=self.handshake_timeout)


def server_from_legacy(server: ConnectionParameters) -> Server:
    return Server(server.host, server.port, server.login, server.unescaped_passcode, server.connect_headers)


def ignore_error(frame: ErrorFrame) -> None:
    del frame


@dataclass(kw_only=True, slots=True)
class LegacyOptions:
    PROTOCOL_VERSION: ClassVar = "1.2"
    servers: list[ConnectionParameters] = field(kw_only=False)
    on_error_frame: Callable[[ErrorFrame], Any] | None = log_error_frame
    heartbeat: Heartbeat = field(default=Heartbeat(1000, 1000))
    ssl: Literal[True] | SSLContext | None = None
    connect_retry_attempts: int = 3
    connect_retry_interval: int = 1
    connect_timeout: int = 2
    read_max_chunk_size: int = 1024 * 1024
    write_retry_attempts: int = 3
    connection_confirmation_timeout: int = 2
    disconnect_confirmation_timeout: int = 2
    check_server_alive_interval_factor: int = 3
    no_message_restart_interval: timedelta | None = timedelta(hours=1)
    keep_alive_on_connection_failure: bool = False
    max_concurrent_handlers: int | None = 100
    max_pending_messages: int | None = None
    max_pending_bytes: int | None = None
    connection_class: type[AbstractConnection] = Connection

    def confirmation(self, timeout: float | None) -> Confirmation:
        return Unconfirmed(self.write_retry_attempts) if timeout is None else Confirmed(timeout)

    def to_config(self) -> RuntimeConfig:
        if self.connect_retry_attempts <= 0:
            raise FailedAllConnectAttemptsError(retry_attempts=self.connect_retry_attempts, issues=[])
        if not self.servers:
            raise FailedAllConnectAttemptsError(
                retry_attempts=self.connect_retry_attempts,
                issues=[AllServersUnavailable(servers=self.servers, timeout=self.connect_timeout)]
                * self.connect_retry_attempts,
            )
        defaults = ConnectionSettings()
        return RuntimeConfig(
            tuple(server_from_legacy(server) for server in self.servers),
            connection=ConnectionSettings(
                # The legacy transport enforces the original deadlines, including
                # expired ones. Native configuration keeps valid positive bounds.
                timeout=self.connect_timeout if self.connect_timeout > 0 else defaults.timeout,
                handshake_timeout=self.connection_confirmation_timeout
                if self.connection_confirmation_timeout > 0
                else defaults.handshake_timeout,
                disconnect=Confirmed(self.disconnect_confirmation_timeout)
                if self.disconnect_confirmation_timeout > 0
                else Unconfirmed(),
                protocol_version=self.PROTOCOL_VERSION,
                read_chunk_size=self.read_max_chunk_size,
                tls=self.ssl or False,
                heartbeat=self.heartbeat,
                heartbeat_tolerance=self.check_server_alive_interval_factor,
                idle_timeout=self.no_message_restart_interval.total_seconds()
                if self.no_message_restart_interval is not None
                else math.inf,
            ),
            recovery=RecoveryPolicy(
                attempts=self.connect_retry_attempts,
                delay=self.connect_retry_interval,
                keep_trying=self.keep_alive_on_connection_failure,
            ),
            delivery=DeliveryLimits(
                concurrency=Unbounded()
                if self.max_concurrent_handlers is None
                else Paused()
                if self.max_concurrent_handlers == 0
                else self.max_concurrent_handlers,
                pending_messages=Unbounded() if self.max_pending_messages is None else self.max_pending_messages,
                pending_bytes=Unbounded() if self.max_pending_bytes is None else self.max_pending_bytes,
            ),
        )

    def to_runtime(
        self,
        *,
        wrap_transport: Callable[[LegacyTransport, ConnectionParameters], Awaitable[Transport]] | None = None,
    ) -> Runtime:
        config = self.to_config()
        paths: dict[int, str | None] = {}
        parameters: dict[int, ConnectionParameters] = {}

        def servers() -> tuple[Server, ...]:
            snapshot = tuple(server_from_legacy(server) for server in self.servers)
            paths.clear()
            paths.update(
                {id(native): legacy.ws_uri_path for native, legacy in zip(snapshot, self.servers, strict=True)}
            )
            parameters.clear()
            parameters.update({id(native): legacy for native, legacy in zip(snapshot, self.servers, strict=True)})
            return snapshot

        factory = LegacyTransportFactory(
            self.connection_class, paths, self.connect_timeout, self.connection_confirmation_timeout
        )

        async def connect(server: Server, settings: ConnectionSettings) -> Transport:
            transport = await factory(server, settings)
            if wrap_transport is None:
                return transport
            try:
                return await wrap_transport(transport, parameters[id(server)])
            except BaseException:
                await transport.close()
                raise

        return Runtime(
            config,
            transport_factory=connect,
            on_error_frame=self._dispatch_error,
            server_source=servers,
            protocol=LegacyProtocol(),
        )

    def _dispatch_error(self, frame: ErrorFrame) -> None:
        if self.on_error_frame:
            self.on_error_frame(frame)
