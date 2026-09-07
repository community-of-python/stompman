"""Public compatibility exports are lazy so importing core never loads a facade."""

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from stompman.client import Client
    from stompman.config import ConnectionParameters, Heartbeat
    from stompman.errors import (
        ConnectionConfirmationTimeout,
        ConnectionLostError,
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
    from stompman.frames import (
        AbortFrame,
        AckFrame,
        AckMode,
        AnyClientFrame,
        AnyRealServerFrame,
        AnyServerFrame,
        BeginFrame,
        CommitFrame,
        ConnectedFrame,
        ConnectFrame,
        DisconnectFrame,
        ErrorFrame,
        HeartbeatFrame,
        MessageFrame,
        NackFrame,
        ReceiptFrame,
        SendFrame,
        SubscribeFrame,
        UnsubscribeFrame,
    )
    from stompman.logger import LOGGER as logger  # ruff: ignore[constant-imported-as-non-constant]
    from stompman.serde import FrameParser, dump_frame
    from stompman.subscription import AckableMessageFrame, AutoAckSubscription, ManualAckSubscription
    from stompman.transaction import Transaction

# Lazy exports keep the compatibility facade out of core imports.
_EXPORTS = {  # ruff: ignore[non-empty-init-module]
    "Client": ("stompman.client", "Client"),
    "ConnectionParameters": ("stompman.config", "ConnectionParameters"),
    "Heartbeat": ("stompman.config", "Heartbeat"),
    "ConnectionConfirmationTimeout": ("stompman.errors", "ConnectionConfirmationTimeout"),
    "ConnectionLostError": ("stompman.errors", "ConnectionLostError"),
    "ConsumerOverloadedError": ("stompman.errors", "ConsumerOverloadedError"),
    "Error": ("stompman.errors", "Error"),
    "FailedAllConnectAttemptsError": ("stompman.errors", "FailedAllConnectAttemptsError"),
    "FailedAllWriteAttemptsError": ("stompman.errors", "FailedAllWriteAttemptsError"),
    "ReceiptRejectedError": ("stompman.errors", "ReceiptRejectedError"),
    "ReceiptTimeoutError": ("stompman.errors", "ReceiptTimeoutError"),
    "StompProtocolConnectionIssue": ("stompman.errors", "StompProtocolConnectionIssue"),
    "SubscriptionError": ("stompman.errors", "SubscriptionError"),
    "TransactionOutcomeUnknownError": ("stompman.errors", "TransactionOutcomeUnknownError"),
    "UnsupportedProtocolVersion": ("stompman.errors", "UnsupportedProtocolVersion"),
    "AbortFrame": ("stompman.frames", "AbortFrame"),
    "AckFrame": ("stompman.frames", "AckFrame"),
    "AckMode": ("stompman.frames", "AckMode"),
    "AnyClientFrame": ("stompman.frames", "AnyClientFrame"),
    "AnyRealServerFrame": ("stompman.frames", "AnyRealServerFrame"),
    "AnyServerFrame": ("stompman.frames", "AnyServerFrame"),
    "BeginFrame": ("stompman.frames", "BeginFrame"),
    "CommitFrame": ("stompman.frames", "CommitFrame"),
    "ConnectedFrame": ("stompman.frames", "ConnectedFrame"),
    "ConnectFrame": ("stompman.frames", "ConnectFrame"),
    "DisconnectFrame": ("stompman.frames", "DisconnectFrame"),
    "ErrorFrame": ("stompman.frames", "ErrorFrame"),
    "HeartbeatFrame": ("stompman.frames", "HeartbeatFrame"),
    "MessageFrame": ("stompman.frames", "MessageFrame"),
    "NackFrame": ("stompman.frames", "NackFrame"),
    "ReceiptFrame": ("stompman.frames", "ReceiptFrame"),
    "SendFrame": ("stompman.frames", "SendFrame"),
    "SubscribeFrame": ("stompman.frames", "SubscribeFrame"),
    "UnsubscribeFrame": ("stompman.frames", "UnsubscribeFrame"),
    "logger": ("stompman.logger", "LOGGER"),
    "FrameParser": ("stompman.serde", "FrameParser"),
    "dump_frame": ("stompman.serde", "dump_frame"),
    "AckableMessageFrame": ("stompman.subscription", "AckableMessageFrame"),
    "AutoAckSubscription": ("stompman.subscription", "AutoAckSubscription"),
    "ManualAckSubscription": ("stompman.subscription", "ManualAckSubscription"),
    "Transaction": ("stompman.transaction", "Transaction"),
}


def __getattr__(name: str) -> Any:  # ruff: ignore[any-type]
    if name not in _EXPORTS:
        raise AttributeError(name)
    module, attribute = _EXPORTS[name]
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


__all__ = [
    "AbortFrame",
    "AckFrame",
    "AckMode",
    "AckableMessageFrame",
    "AnyClientFrame",
    "AnyRealServerFrame",
    "AnyServerFrame",
    "AutoAckSubscription",
    "BeginFrame",
    "Client",
    "CommitFrame",
    "ConnectFrame",
    "ConnectedFrame",
    "ConnectionConfirmationTimeout",
    "ConnectionLostError",
    "ConnectionParameters",
    "ConsumerOverloadedError",
    "DisconnectFrame",
    "Error",
    "ErrorFrame",
    "FailedAllConnectAttemptsError",
    "FailedAllWriteAttemptsError",
    "FrameParser",
    "Heartbeat",
    "HeartbeatFrame",
    "ManualAckSubscription",
    "MessageFrame",
    "NackFrame",
    "ReceiptFrame",
    "ReceiptRejectedError",
    "ReceiptTimeoutError",
    "SendFrame",
    "StompProtocolConnectionIssue",
    "SubscribeFrame",
    "SubscriptionError",
    "Transaction",
    "TransactionOutcomeUnknownError",
    "UnsubscribeFrame",
    "UnsupportedProtocolVersion",
    "dump_frame",
    "logger",
]
