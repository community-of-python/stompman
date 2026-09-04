from dataclasses import dataclass, field
from typing import Literal

from stompman.config import ConnectionParameters  # ruff: ignore[typing-only-first-party-import]
from stompman.frames import ErrorFrame, HeartbeatFrame, MessageFrame, ReceiptFrame


@dataclass(kw_only=True)
class Error(Exception):
    def __str__(self) -> str:
        return self.__repr__()


@dataclass(kw_only=True)
class ConnectionLostError(Error):
    """Raised in stompman.AbstractConnection—and handled in stompman.ConnectionManager, therefore is private."""

    reason: Exception | str


@dataclass(frozen=True, kw_only=True, slots=True)
class ConnectionConfirmationTimeout:
    timeout: int
    frames: list[MessageFrame | ReceiptFrame | ErrorFrame | HeartbeatFrame]


@dataclass(frozen=True, kw_only=True, slots=True)
class UnsupportedProtocolVersion:
    given_version: str
    supported_version: str


@dataclass(frozen=True, kw_only=True, slots=True)
class ConnectionLostOnLifespanEnter: ...


@dataclass(frozen=True, kw_only=True, slots=True)
class AllServersUnavailable:
    servers: list["ConnectionParameters"]
    timeout: int


StompProtocolConnectionIssue = ConnectionConfirmationTimeout | UnsupportedProtocolVersion
AnyConnectionIssue = StompProtocolConnectionIssue | ConnectionLostOnLifespanEnter | AllServersUnavailable


@dataclass(kw_only=True)
class FailedAllConnectAttemptsError(Error):
    retry_attempts: int
    issues: list[AnyConnectionIssue]


@dataclass(kw_only=True)
class FailedAllWriteAttemptsError(Error):
    retry_attempts: int


@dataclass(kw_only=True)
class SubscriptionError(Error):
    """A receipt-confirmed subscription could not be established or restored."""

    subscription_id: str
    reason: Literal["rejected", "timeout", "connection_lost", "unsubscribed"]
    frame: ErrorFrame | None = field(default=None, repr=False)
