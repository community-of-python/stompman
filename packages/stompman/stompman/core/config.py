from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from ssl import SSLContext
from typing import Any, Literal

from stompman.config import ConnectionParameters, Heartbeat
from stompman.connection import AbstractConnection, Connection
from stompman.frames import ErrorFrame
from stompman.logger import LOGGER


def log_error_frame(frame: ErrorFrame) -> None:
    LOGGER.error("received error frame: %s", frame)


@dataclass(kw_only=True, slots=True)
class RuntimeConfig:
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

    def validate(self) -> None:
        if not self.servers:
            msg = "at least one server is required"
            raise ValueError(msg)
        positive = {
            "connect_retry_attempts": self.connect_retry_attempts,
            "write_retry_attempts": self.write_retry_attempts,
            "read_max_chunk_size": self.read_max_chunk_size,
            "check_server_alive_interval_factor": self.check_server_alive_interval_factor,
            "max_pending_messages": self.max_pending_messages,
            "max_pending_bytes": self.max_pending_bytes,
        }
        if self.max_concurrent_handlers is not None:
            positive["max_concurrent_handlers"] = self.max_concurrent_handlers
        for name, value in positive.items():
            if value <= 0:
                msg = f"{name} must be positive"
                raise ValueError(msg)
        if (
            min(
                self.connect_timeout,
                self.connect_retry_interval,
                self.connection_confirmation_timeout,
                self.disconnect_confirmation_timeout,
                self.heartbeat.will_send_interval_ms,
                self.heartbeat.want_to_receive_interval_ms,
            )
            < 0
        ):
            msg = "timeouts and heartbeat intervals must be nonnegative"
            raise ValueError(msg)
        if self.no_message_restart_interval is not None and self.no_message_restart_interval.total_seconds() <= 0:
            msg = "no_message_restart_interval must be positive or None"
            raise ValueError(msg)
