"""STOMP frame semantics, checked before an operation acquires resources."""

from collections.abc import Mapping

from .errors import ProtocolError
from .frames import (
    AbortFrame,
    AckFrame,
    AnyBodyFrame,
    AnyClientFrame,
    AnyCommandFrame,
    BeginFrame,
    CommitFrame,
    ConnectedFrame,
    ConnectFrame,
    MessageFrame,
    NackFrame,
    ReceiptFrame,
    SendFrame,
    StompFrame,
    SubscribeFrame,
    UnsubscribeFrame,
)

REQUIRED_HEADERS: Mapping[type[AnyCommandFrame], tuple[str, ...]] = {
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
_LITERAL_HEADER_FRAMES = (ConnectFrame, ConnectedFrame)
_ACK_MODES = frozenset({"auto", "client", "client-individual"})


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


def _validate_header(name: object, value: object, *, literal: bool) -> None:
    if not isinstance(name, str) or not isinstance(value, str) or not name or "\x00" in name or "\x00" in value:
        raise ProtocolError(reason="headers require a nonempty UTF-8 name and a value without NUL")
    try:
        name.encode()
        value.encode()
    except UnicodeEncodeError as error:
        raise ProtocolError(reason="header is not valid UTF-8") from error
    if literal and (any(char in name for char in "\r\n:") or any(char in value for char in "\r\n")):
        raise ProtocolError(reason="CONNECT and CONNECTED headers cannot contain line delimiters")


def _validate_body(frame: AnyCommandFrame) -> None:
    body = frame.body if isinstance(frame, AnyBodyFrame) else b""
    length = content_length(frame.headers)
    if length is not None and length != len(body):
        raise ProtocolError(reason="content-length must equal the body octet count")
    if length is None and b"\x00" in body:
        raise ProtocolError(reason="a body containing NUL requires content-length")


def validate_frame(frame: AnyCommandFrame) -> None:
    literal = isinstance(frame, _LITERAL_HEADER_FRAMES)
    for name, value in frame.headers.items():
        _validate_header(name, value, literal=literal)
    for name in REQUIRED_HEADERS.get(type(frame), ()):
        require_header(frame.headers, name)
    if isinstance(frame, SubscribeFrame) and frame.headers.get("ack", "auto") not in _ACK_MODES:
        raise ProtocolError(reason="unsupported acknowledgement mode")
    _validate_body(frame)


def validate_outgoing(frame: AnyClientFrame) -> None:
    if not isinstance(frame, AnyClientFrame):
        raise ProtocolError(reason="only client commands may be sent to a server")
    validate_frame(frame)
