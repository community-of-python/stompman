"""Immutable native configuration. Compatibility choices belong to adapters."""

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from ssl import SSLContext
from types import MappingProxyType
from typing import Self, final

MAX_PORT = 65535


def positive_integer(name: str, value: int) -> None:
    if type(value) is not int or value <= 0:
        msg = f"{name} must be a positive integer"
        raise ValueError(msg)


def positive(name: str, value: float) -> None:
    if not math.isfinite(value) or value <= 0:
        msg = f"{name} must be a finite positive number"
        raise ValueError(msg)


@dataclass(frozen=True, slots=True)
class Heartbeat:
    will_send_interval_ms: int
    want_to_receive_interval_ms: int

    def __post_init__(self) -> None:
        for value in (self.will_send_interval_ms, self.want_to_receive_interval_ms):
            if type(value) is not int or value < 0:
                msg = "heartbeat intervals must be nonnegative integers"
                raise ValueError(msg)

    def to_header(self) -> str:
        return f"{self.will_send_interval_ms},{self.want_to_receive_interval_ms}"

    @classmethod
    def from_header(cls, header: str) -> Self:
        send, receive = header.split(",", maxsplit=1)
        return cls(int(send), int(receive))

    def negotiate(self, peer: "Heartbeat") -> "Heartbeat":
        return Heartbeat(
            max(self.will_send_interval_ms, peer.want_to_receive_interval_ms)
            if self.will_send_interval_ms and peer.want_to_receive_interval_ms
            else 0,
            max(self.want_to_receive_interval_ms, peer.will_send_interval_ms)
            if self.want_to_receive_interval_ms and peer.will_send_interval_ms
            else 0,
        )


@dataclass(frozen=True, slots=True)
class Server:
    host: str
    port: int
    login: str
    passcode: str = field(repr=False)
    connect_headers: Mapping[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self.host or type(self.port) is not int or not 0 < self.port <= MAX_PORT:
            msg = "server requires a host and a port between 1 and 65535"
            raise ValueError(msg)
        object.__setattr__(self, "connect_headers", MappingProxyType(dict(self.connect_headers)))


@dataclass(frozen=True, slots=True)
class Confirmed:
    """Wait for this operation's receipt; never replay an ambiguous write."""

    timeout: float = 5.0

    def __post_init__(self) -> None:
        positive("receipt timeout", self.timeout)

    @property
    def attempts(self) -> int:
        """A confirmed command is never automatically replayed."""
        return 1


@dataclass(frozen=True, slots=True)
class Unconfirmed:
    """Explicitly accept uncertain delivery and possible duplicates on retry."""

    attempts: int = 1

    def __post_init__(self) -> None:
        positive_integer("write attempts", self.attempts)


Confirmation = Confirmed | Unconfirmed
DEFAULT_CONFIRMATION = Confirmed()


@dataclass(frozen=True, slots=True, kw_only=True)
class FrameLimits:
    header_count: int = 1024
    line_bytes: int = 16 * 1024
    header_bytes: int = 64 * 1024
    body_bytes: int = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        for name in ("header_count", "line_bytes", "header_bytes", "body_bytes"):
            positive_integer(name, getattr(self, name))


DEFAULT_FRAME_LIMITS = FrameLimits()


@dataclass(frozen=True, slots=True, kw_only=True)
class ConnectionSettings:
    timeout: float = 2.0
    handshake_timeout: float = 2.0
    disconnect: Confirmation = Confirmed(2)
    protocol_version: str = "1.2"
    read_chunk_size: int = 1024 * 1024
    tls: bool | SSLContext = False
    heartbeat: Heartbeat = Heartbeat(1000, 1000)
    heartbeat_tolerance: float = 3.0
    idle_timeout: float = math.inf
    frame_limits: FrameLimits = FrameLimits()

    def __post_init__(self) -> None:
        for name in ("timeout", "handshake_timeout", "heartbeat_tolerance"):
            positive(name, getattr(self, name))
        if not self.protocol_version:
            msg = "protocol version must not be empty"
            raise ValueError(msg)
        positive_integer("read_chunk_size", self.read_chunk_size)
        if self.idle_timeout != math.inf:
            positive("idle_timeout", self.idle_timeout)


@dataclass(frozen=True, slots=True, kw_only=True)
class RecoveryPolicy:
    attempts: int = 3
    delay: float = 1.0
    keep_trying: bool = False

    def __post_init__(self) -> None:
        positive_integer("connect attempts", self.attempts)
        if not math.isfinite(self.delay) or self.delay < 0:
            msg = "retry delay must be finite and nonnegative"
            raise ValueError(msg)


@final
@dataclass(frozen=True, slots=True)
class Unbounded:
    """Explicitly disable one delivery limit."""


@final
@dataclass(frozen=True, slots=True)
class Paused:
    """Accept deliveries without starting handlers."""


@dataclass(frozen=True, slots=True, kw_only=True)
class DeliveryLimits:
    concurrency: int | Unbounded | Paused = 100
    pending_messages: int | Unbounded = 1024
    pending_bytes: int | Unbounded = 64 * 1024 * 1024

    def __post_init__(self) -> None:
        if not isinstance(self.concurrency, (Unbounded, Paused)):
            positive_integer("concurrency", self.concurrency)
        for name in ("pending_messages", "pending_bytes"):
            value = getattr(self, name)
            if not isinstance(value, Unbounded):
                positive_integer(name, value)


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    servers: tuple[Server, ...]
    connection: ConnectionSettings = ConnectionSettings()
    recovery: RecoveryPolicy = RecoveryPolicy()
    delivery: DeliveryLimits = DeliveryLimits()

    def __post_init__(self) -> None:
        if not self.servers:
            msg = "at least one server is required"
            raise ValueError(msg)
        if not all(isinstance(server, Server) for server in self.servers):
            msg = "native configuration requires core.Server values"
            raise TypeError(msg)
        object.__setattr__(self, "servers", tuple(self.servers))
