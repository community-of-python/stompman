"""Legacy exception families and their original diagnostic payloads."""

from dataclasses import dataclass

from .config import ConnectionParameters
from .core.errors import AllServersUnavailable as NativeAllServersUnavailable
from .core.errors import (
    ConnectionConfirmationTimeout,
    ConnectionLostError,
    ConnectionLostOnLifespanEnter,
    ConsumerOverloadedError,
    Error,
    FailedAllWriteAttemptsError,
    ReceiptRejectedError,
    ReceiptTimeoutError,
    SubscriptionError,
    TransactionOutcomeUnknownError,
    UnsupportedProtocolVersion,
)
from .core.errors import FailedAllConnectAttemptsError as NativeFailedAllConnectAttemptsError


@dataclass(frozen=True, kw_only=True, slots=True)
class AllServersUnavailable(NativeAllServersUnavailable):
    # The compatibility boundary restores the original payload type. The native
    # diagnostic deliberately contains only native Server objects.
    servers: list[ConnectionParameters]  # type: ignore[assignment]
    timeout: int


StompProtocolConnectionIssue = ConnectionConfirmationTimeout | UnsupportedProtocolVersion
AnyConnectionIssue = StompProtocolConnectionIssue | ConnectionLostOnLifespanEnter | AllServersUnavailable


@dataclass(kw_only=True)
class FailedAllConnectAttemptsError(NativeFailedAllConnectAttemptsError):
    issues: list[AnyConnectionIssue]  # type: ignore[assignment]


__all__ = [
    "AllServersUnavailable",
    "AnyConnectionIssue",
    "ConnectionConfirmationTimeout",
    "ConnectionLostError",
    "ConnectionLostOnLifespanEnter",
    "ConsumerOverloadedError",
    "Error",
    "FailedAllConnectAttemptsError",
    "FailedAllWriteAttemptsError",
    "ReceiptRejectedError",
    "ReceiptTimeoutError",
    "StompProtocolConnectionIssue",
    "SubscriptionError",
    "TransactionOutcomeUnknownError",
    "UnsupportedProtocolVersion",
]
