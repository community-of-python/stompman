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
    """A physical transport failed; the runtime decides whether recovery is safe."""

    reason: Exception | str


@dataclass(frozen=True, kw_only=True, slots=True)
class ConnectionConfirmationTimeout:
    timeout: float
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
    timeout: float


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
class ReceiptTimeoutError(Error):
    """The broker may have accepted the operation; automatic replay is unsafe."""

    receipt_id: str
    timeout: float


@dataclass(kw_only=True)
class ReceiptRejectedError(Error):
    receipt_id: str
    frame: ErrorFrame = field(repr=False)


@dataclass(kw_only=True)
class SubscriptionError(Error):
    """A receipt-confirmed subscription could not be established or restored."""

    subscription_id: str
    reason: Literal["rejected", "timeout", "connection_lost", "unsubscribed"]
    frame: ErrorFrame | None = field(default=None, repr=False)


@dataclass(kw_only=True)
class TransactionOutcomeUnknownError(Error):
    transaction_id: str
    reason: Exception


@dataclass(kw_only=True)
class ConsumerOverloadedError(Error):
    """Local delivery admission is exhausted. Configure broker credit and capacity."""

    max_pending_messages: int
    max_pending_bytes: int
