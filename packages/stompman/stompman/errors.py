"""Compatibility exports for :mod:`stompman.core.errors`."""

from .core.errors import (
    AllServersUnavailable,
    AnyConnectionIssue,
    ConnectionConfirmationTimeout,
    ConnectionLostError,
    ConnectionLostOnLifespanEnter,
    ConsumerOverloadedError,
    Error,
    FailedAllConnectAttemptsError,
    FailedAllWriteAttemptsError,
    ReceiptRejectedError,
    ReceiptTimeoutError,
    StompProtocolConnectionIssue,
    SubscriptionError,
    TransactionOutcomeUnknownError,
    UnsupportedProtocolVersion,
)

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
