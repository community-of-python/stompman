import struct
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Final, cast, final

from .frames import (
    AbortFrame,
    AckFrame,
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
    StompFrame,
    SubscribeFrame,
    UnsubscribeFrame,
)

NEWLINE: Final = b"\n"
CARRIAGE: Final = b"\r"
NULL: Final = b"\x00"
_NULL_BYTE: Final = ord(NULL)
_NEWLINE_BYTE: Final = ord(NEWLINE)
BACKSLASH = b"\\"
COLON_ = b":"

HEADER_ESCAPE_CHARS: Final = {
    NEWLINE.decode(): "\\n",
    COLON_.decode(): "\\c",
    BACKSLASH.decode(): "\\\\",
    CARRIAGE.decode(): "\\r",
}
HEADER_UNESCAPE_CHARS: Final = {
    b"n": NEWLINE,
    b"r": CARRIAGE,
    b"c": COLON_,
    BACKSLASH: BACKSLASH,
}


def iter_bytes(bytes_: bytes | bytearray) -> tuple[bytes, ...]:
    return struct.unpack(f"{len(bytes_)!s}c", bytes_)


COMMANDS_TO_FRAMES: Final[dict[bytes, type[AnyClientFrame | AnyServerFrame]]] = {
    # Client frames
    b"SEND": SendFrame,
    b"SUBSCRIBE": SubscribeFrame,
    b"UNSUBSCRIBE": UnsubscribeFrame,
    b"BEGIN": BeginFrame,
    b"COMMIT": CommitFrame,
    b"ABORT": AbortFrame,
    b"ACK": AckFrame,
    b"NACK": NackFrame,
    b"DISCONNECT": DisconnectFrame,
    b"CONNECT": ConnectFrame,
    b"STOMP": StompFrame,
    # Server frames
    b"CONNECTED": ConnectedFrame,
    b"MESSAGE": MessageFrame,
    b"RECEIPT": ReceiptFrame,
    b"ERROR": ErrorFrame,
}
FRAMES_TO_COMMANDS: Final = {value: key for key, value in COMMANDS_TO_FRAMES.items()}
FRAMES_WITH_BODY: Final = (SendFrame, MessageFrame, ErrorFrame)


def dump_header(key: str, value: str) -> bytes:
    escaped_key = "".join(HEADER_ESCAPE_CHARS.get(char, char) for char in key)
    escaped_value = "".join(HEADER_ESCAPE_CHARS.get(char, char) for char in value)
    return f"{escaped_key}:{escaped_value}\n".encode()


def dump_frame(frame: AnyClientFrame | AnyRealServerFrame) -> bytes:
    sorted_headers = sorted(frame.headers.items())
    dumped_headers = (
        (f"{key}:{value}\n".encode() for key, value in sorted_headers)
        if isinstance(frame, (ConnectFrame, ConnectedFrame))
        else (dump_header(key, cast("str", value)) for key, value in sorted_headers)
    )
    lines = (
        FRAMES_TO_COMMANDS[type(frame)],
        NEWLINE,
        *dumped_headers,
        NEWLINE,
        frame.body if isinstance(frame, FRAMES_WITH_BODY) else b"",
        NULL,
    )
    return b"".join(lines)


def unescape_byte(*, byte: bytes, previous_byte: bytes | None) -> bytes | None:
    if previous_byte == BACKSLASH:
        return HEADER_UNESCAPE_CHARS.get(byte)
    if byte == BACKSLASH:
        return None
    return byte


def _unescape_header_part(value: bytes) -> bytes | None:
    result = bytearray()
    iterator = iter(iter_bytes(value))
    for byte in iterator:
        if byte == BACKSLASH:
            replacement = HEADER_UNESCAPE_CHARS.get(next(iterator, b""))
            if replacement is None:
                return None
            result.extend(replacement)
        elif byte == COLON_:
            return None
        else:
            result.extend(byte)
    return bytes(result)


def parse_header(buffer: bytearray, *, unescape: bool = True) -> tuple[str, str] | None:
    key, separator, value = bytes(buffer).removesuffix(NEWLINE).removesuffix(CARRIAGE).partition(COLON_)
    if not separator:
        return None
    if unescape:
        unescaped_key = _unescape_header_part(key)
        unescaped_value = _unescape_header_part(value)
        if unescaped_key is None or unescaped_value is None:
            return None
        key, value = unescaped_key, unescaped_value
    with suppress(UnicodeDecodeError):
        return key.decode(), value.decode()
    return None


def make_frame_from_parts(*, command: bytes, headers: dict[str, str], body: bytes) -> AnyClientFrame | AnyServerFrame:
    frame_type = COMMANDS_TO_FRAMES[command]
    headers_ = cast("Any", headers)
    return frame_type(headers=headers_, body=body) if frame_type in FRAMES_WITH_BODY else frame_type(headers=headers_)  # type: ignore[call-arg]


class _CommandLine(Enum):
    WAITING = auto()


@final
@dataclass(frozen=True, kw_only=True, slots=True)
class _ReadingHeaders:
    command: bytes
    headers: dict[str, str]


@final
@dataclass(frozen=True, kw_only=True, slots=True)
class _ReadingBody:
    command: bytes
    headers: dict[str, str]
    content_length: int | None


def _body_length(headers: dict[str, str]) -> int | None:
    content_length = None
    for header_name, header_value in headers.items():
        if header_name.lower() == "content-length":
            with suppress(ValueError):
                candidate = int(header_value)
                if candidate >= 0:
                    content_length = candidate
    return content_length


@dataclass(kw_only=True, slots=True, init=False)
class FrameParser:
    """Decode a byte stream while retaining only its current line or body.

    Each phase carries the data that is already known. Header dictionaries move
    into emitted frames; later input never mutates a previously emitted frame.
    """

    _buffer: bytearray
    _state: _CommandLine | _ReadingHeaders | _ReadingBody

    def __init__(self) -> None:
        self._reset()

    def _reset(self) -> None:
        self._buffer = bytearray()
        self._state = _CommandLine.WAITING

    def _read_line(self, state: _CommandLine | _ReadingHeaders) -> HeartbeatFrame | None:
        line = self._buffer.removesuffix(CARRIAGE)
        self._buffer.clear()
        if isinstance(state, _CommandLine):
            if not line:
                return HeartbeatFrame()
            command = bytes(line)
            if command in COMMANDS_TO_FRAMES:
                self._state = _ReadingHeaders(command=command, headers={})
        elif not line:
            self._state = _ReadingBody(
                command=state.command, headers=state.headers, content_length=_body_length(state.headers)
            )
        else:
            header = parse_header(line, unescape=state.command not in {b"CONNECT", b"CONNECTED"})
            if header is not None:
                header_name, header_value = header
                state.headers.setdefault(header_name, header_value)
        return None

    def _read_body(self, state: _ReadingBody, byte: int) -> AnyClientFrame | AnyServerFrame | None:
        if byte == _NULL_BYTE:
            if state.content_length is None or len(self._buffer) == state.content_length:
                frame = make_frame_from_parts(command=state.command, headers=state.headers, body=bytes(self._buffer))
                self._reset()
                return frame
            self._buffer.append(byte)
        elif state.content_length is None or len(self._buffer) < state.content_length:
            self._buffer.append(byte)
        return None

    def parse_frames_from_chunk(self, chunk: bytes) -> Iterator[AnyClientFrame | AnyServerFrame]:
        for byte in chunk:
            state = self._state
            if isinstance(state, _ReadingBody):
                frame = self._read_body(state, byte)
                if frame is not None:
                    yield frame
            elif byte == _NULL_BYTE:
                self._reset()
            elif byte == _NEWLINE_BYTE:
                heartbeat = self._read_line(state)
                if heartbeat is not None:
                    yield heartbeat
            else:
                self._buffer.append(byte)
