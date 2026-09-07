"""Translate legacy defaults and transport conventions at the facade seam."""

import math
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from ssl import SSLContext
from typing import Any, Literal, Protocol, runtime_checkable

from .config import ConnectionParameters, Heartbeat
from .connection import AbstractConnection, Connection
from .core.config import (
    Confirmation,
    Confirmed,
    ConnectionSettings,
    DeliveryLimits,
    RecoveryPolicy,
    RuntimeConfig,
    Server,
    Unconfirmed,
)
from .core.errors import ConnectionLostError
from .core.frames import AnyClientFrame, AnyServerFrame, ErrorFrame
from .core.runtime import Runtime, log_error_frame
from .core.transport import Transport


@runtime_checkable
class AsyncHeartbeat(Protocol):
    async def send_heartbeat(self) -> None: ...


class LegacyTransport:
    def __init__(self, connection: AbstractConnection) -> None:
        self.connection = connection
        self._opened_at = time.monotonic()

    @property
    def last_received_at(self) -> float:
        last_read = self.connection.last_read_time
        return self._opened_at if last_read is None else time.monotonic() - max(0, time.time() - last_read)

    async def write_frame(self, frame: AnyClientFrame) -> None:
        await self.connection.write_frame(frame)

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

    async def __call__(self, server: Server, settings: ConnectionSettings) -> Transport:
        connection = await self.connection_class.connect(
            host=server.host,
            port=server.port,
            timeout=settings.timeout,
            read_max_chunk_size=settings.read_chunk_size,
            ssl=settings.tls or None,
            ws_uri_path=self.paths.get(id(server)),
        )
        if connection is None:
            raise ConnectionLostError(reason="transport could not connect")
        return LegacyTransport(connection)


def server_from_legacy(server: ConnectionParameters) -> Server:
    return Server(server.host, server.port, server.login, server.unescaped_passcode, server.connect_headers)


def ignore_error(frame: ErrorFrame) -> None:
    del frame


@dataclass(kw_only=True, slots=True)
class LegacyOptions:
    servers: list[ConnectionParameters] = field(kw_only=False)
    on_error_frame: Callable[[ErrorFrame], Any] | None = log_error_frame
    heartbeat: Heartbeat = field(default=Heartbeat(1000, 1000))
    ssl: Literal[True] | SSLContext | None = None
    connect_retry_attempts: int = 3
    connect_retry_interval: float = 1
    connect_timeout: float = 2
    read_max_chunk_size: int = 1024 * 1024
    write_retry_attempts: int = 3
    connection_confirmation_timeout: float = 2
    disconnect_confirmation_timeout: float = 2
    check_server_alive_interval_factor: float = 3
    no_message_restart_interval: timedelta | None = timedelta(hours=1)
    keep_alive_on_connection_failure: bool = False
    max_concurrent_handlers: int | None = 100
    max_pending_messages: int = 1024
    max_pending_bytes: int = 64 * 1024 * 1024
    connection_class: type[AbstractConnection] = Connection

    def confirmation(self, timeout: float | None) -> Confirmation:
        return Unconfirmed(self.write_retry_attempts) if timeout is None else Confirmed(timeout)

    def to_config(self) -> RuntimeConfig:
        Unconfirmed(self.write_retry_attempts)
        return RuntimeConfig(
            tuple(server_from_legacy(server) for server in self.servers),
            connection=ConnectionSettings(
                timeout=self.connect_timeout,
                handshake_timeout=self.connection_confirmation_timeout,
                disconnect_timeout=self.disconnect_confirmation_timeout,
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
                concurrency=self.max_concurrent_handlers
                if self.max_concurrent_handlers is not None
                else self.max_pending_messages,
                pending_messages=self.max_pending_messages,
                pending_bytes=self.max_pending_bytes,
            ),
        )

    def to_runtime(self) -> Runtime:
        config = self.to_config()
        factory = LegacyTransportFactory(
            self.connection_class,
            {id(native): legacy.ws_uri_path for native, legacy in zip(config.servers, self.servers, strict=True)},
        )
        return Runtime(config, transport_factory=factory, on_error_frame=self.on_error_frame or ignore_error)
