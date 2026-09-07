"""STOMP frame semantics, checked before an operation acquires resources."""

from collections.abc import Mapping

from .errors import ProtocolError
from .frames import (
    AbortFrame,
    AckFrame,
    AnyClientFrame,
    AnyRealServerFrame,
    BeginFrame,
    CommitFrame,
    ConnectedFrame,
    ConnectFrame,
    ErrorFrame,
    MessageFrame,
    NackFrame,
    ReceiptFrame,
    SendFrame,
    StompFrame,
    SubscribeFrame,
    UnsubscribeFrame,
)

REQUIRED_HEADERS: Mapping[type[AnyClientFrame | AnyRealServerFrame], tuple[str, ...]] = {
    ConnectFrame: ("accept-version", "host"),
    StompFrame: ("accept-version", "host"),
    ConnectedFrame: ("version",),
    SendFrame: ("destination",),
    SubscribeFrame: ("destination", "id"),
    UnsubscribeFrame: ("id",),
    AckFrame: ("id",),
    NackFrame: ("id",),
    BeginFrame: ("transaction",),
    CommitFrame: ("transaction",),
    AbortFrame: ("transaction",),
    MessageFrame: ("destination", "message-id", "subscription"),
    ReceiptFrame: ("receipt-id",),
}


def content_length(headers: Mapping[str, object]) -> int | None:
    value = headers.get("content-length")
    if "content-length" not in headers:
        return None
    if not isinstance(value, str) or not value or not value.isascii() or not value.isdecimal():
        raise ProtocolError(reason="content-length must be a nonnegative decimal octet count")
    try:
        return int(value)
    except ValueError as error:
        raise ProtocolError(reason="content-length is too large") from error


def require_header(headers: Mapping[str, object], name: str) -> str:
    value = headers.get(name)
    if not isinstance(value, str):
        raise ProtocolError(reason=f"missing required {name} header")
    return value


def validate_frame(frame: AnyClientFrame | AnyRealServerFrame) -> None:
    for key, value in frame.headers.items():
        if not isinstance(key, str) or not isinstance(value, str) or not key or "\x00" in key or "\x00" in value:
            raise ProtocolError(reason="headers require a nonempty UTF-8 name and a value without NUL")
        try:
            key.encode("utf-8")
            value.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ProtocolError(reason="header is not valid UTF-8") from error
        if isinstance(frame, (ConnectFrame, ConnectedFrame)) and (
            any(char in key for char in "\r\n:") or any(char in value for char in "\r\n")
        ):
            raise ProtocolError(reason="CONNECT and CONNECTED headers cannot contain line delimiters")
    for name in REQUIRED_HEADERS.get(type(frame), ()):
        require_header(frame.headers, name)
    if isinstance(frame, SubscribeFrame) and frame.headers.get("ack", "auto") not in {
        "auto",
        "client",
        "client-individual",
    }:
        raise ProtocolError(reason="unsupported acknowledgement mode")
    length = content_length(frame.headers)
    body = frame.body if isinstance(frame, (SendFrame, MessageFrame, ErrorFrame)) else b""
    if length is not None and length != len(body):
        raise ProtocolError(reason="content-length must equal the body octet count")
    if b"\x00" in body and length is None:
        raise ProtocolError(reason="a body containing NUL requires content-length")


def validate_outgoing(frame: AnyClientFrame) -> None:
    if not isinstance(frame, AnyClientFrame):
        raise ProtocolError(reason="only client commands may be sent to a server")
    validate_frame(frame)
