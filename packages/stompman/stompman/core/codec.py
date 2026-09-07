"""STOMP wire encoding and strict incremental decoding."""

import struct
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Final, cast

from .config import DEFAULT_FRAME_LIMITS, FrameLimits
from .errors import ProtocolError
from .frames import (
    AbortFrame,
    AckFrame,
    AnyClientFrame,
    AnyCommandFrame,
    AnyFrame,
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
from .validation import content_length, validate_outgoing

NEWLINE: Final = b"\n"
CARRIAGE: Final = b"\r"
NULL: Final = b"\x00"
BACKSLASH: Final = b"\\"
COLON_: Final = b":"

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

COMMANDS_TO_FRAMES: Final[dict[bytes, type[AnyCommandFrame]]] = {
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
    b"CONNECTED": ConnectedFrame,
    b"MESSAGE": MessageFrame,
    b"RECEIPT": ReceiptFrame,
    b"ERROR": ErrorFrame,
}
FRAMES_TO_COMMANDS: Final = {frame: command for command, frame in COMMANDS_TO_FRAMES.items()}
FRAMES_WITH_BODY: Final = (SendFrame, MessageFrame, ErrorFrame)


def iter_bytes(bytes_: bytes | bytearray) -> tuple[bytes, ...]:
    return struct.unpack(f"{len(bytes_)}c", bytes_)


def dump_header(key: str, value: str) -> bytes:
    escaped_key = "".join(HEADER_ESCAPE_CHARS.get(char, char) for char in key)
    escaped_value = "".join(HEADER_ESCAPE_CHARS.get(char, char) for char in value)
    return f"{escaped_key}:{escaped_value}\n".encode()


def dump_frame(frame: AnyCommandFrame) -> bytes:
    headers = sorted(frame.headers.items())
    dumped_headers = (
        (f"{key}:{value}\n".encode() for key, value in headers)
        if isinstance(frame, (ConnectFrame, ConnectedFrame))
        else (dump_header(key, cast("str", value)) for key, value in headers)
    )
    body = frame.body if isinstance(frame, FRAMES_WITH_BODY) else b""
    return b"".join((FRAMES_TO_COMMANDS[type(frame)], NEWLINE, *dumped_headers, NEWLINE, body, NULL))


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


def make_frame_from_parts(*, command: bytes, headers: dict[str, str], body: bytes) -> AnyCommandFrame:
    frame_type = COMMANDS_TO_FRAMES[command]
    typed_headers = cast("Any", headers)
    if frame_type in FRAMES_WITH_BODY:
        return frame_type(headers=typed_headers, body=body)
    return frame_type(headers=typed_headers)  # type: ignore[call-arg]


class _CommandLine(Enum):
    WAITING = auto()


@dataclass(slots=True, kw_only=True)
class _Headers:
    command: bytes
    values: dict[str, str]
    count: int
    size: int


@dataclass(frozen=True, slots=True, kw_only=True)
class _Body:
    command: bytes
    headers: dict[str, str]
    length: int | None


class FrameDecoder:
    def __init__(self, limits: FrameLimits = DEFAULT_FRAME_LIMITS) -> None:
        self._limits = limits
        self._buffer = bytearray()
        self._state: _CommandLine | _Headers | _Body = _CommandLine.WAITING

    @staticmethod
    def _limit(size: int, maximum: int, name: str) -> None:
        if size > maximum:
            raise ProtocolError(reason=f"{name} limit of {maximum} exceeded")

    def _line(self, state: _CommandLine | _Headers) -> HeartbeatFrame | None:
        line = self._buffer.removesuffix(b"\r")
        self._buffer.clear()
        if b"\r" in line:
            raise ProtocolError(reason="unescaped carriage return in frame line")
        if isinstance(state, _CommandLine):
            if not line:
                return HeartbeatFrame()
            command = bytes(line)
            if command not in COMMANDS_TO_FRAMES:
                raise ProtocolError(reason="unknown frame command")
            self._state = _Headers(command=command, values={}, count=0, size=0)
        elif not line:
            length = content_length(state.values)
            if length is not None:
                self._limit(length, self._limits.body_bytes, "body")
            self._state = _Body(command=state.command, headers=state.values, length=length)
        else:
            state.count += 1
            self._limit(state.count, self._limits.header_count, "header count")
            header = parse_header(line, unescape=state.command not in {b"CONNECT", b"CONNECTED"})
            if header is None or not header[0]:
                raise ProtocolError(reason="malformed header or undefined escape sequence")
            name, value = header
            state.values.setdefault(name, value)
        return None

    def _read_line(
        self, state: _CommandLine | _Headers, chunk: bytes, offset: int
    ) -> tuple[int, HeartbeatFrame | None]:
        newline = chunk.find(b"\n", offset)
        end = len(chunk) if newline < 0 else newline
        if isinstance(state, _Headers):
            state.size += end - offset + (newline >= 0)
            self._limit(state.size, self._limits.header_bytes, "headers")
        self._limit(len(self._buffer) + end - offset, self._limits.line_bytes, "line")
        if chunk.find(b"\x00", offset, end) >= 0:
            raise ProtocolError(reason="NUL before frame body")
        self._buffer.extend(chunk[offset:end])
        if newline < 0:
            return end, None
        return end + 1, self._line(state)

    def _body(self, state: _Body, chunk: bytes, offset: int) -> tuple[int, AnyCommandFrame | None]:
        end = chunk.find(b"\x00", offset) if state.length is None else offset + state.length - len(self._buffer)
        if end < 0 or end >= len(chunk):
            end = len(chunk)
        self._limit(len(self._buffer) + end - offset, self._limits.body_bytes, "body")
        self._buffer.extend(chunk[offset:end])
        if end == len(chunk):
            return end, None
        if chunk[end] != 0:
            raise ProtocolError(reason="body must be followed immediately by NUL")
        if self._buffer and COMMANDS_TO_FRAMES[state.command] not in FRAMES_WITH_BODY:
            raise ProtocolError(reason="only SEND, MESSAGE, and ERROR may have a body")
        frame = make_frame_from_parts(command=state.command, headers=state.headers, body=bytes(self._buffer))
        self._buffer.clear()
        self._state = _CommandLine.WAITING
        return end + 1, frame

    def feed(self, chunk: bytes) -> Iterator[AnyFrame]:
        offset = 0
        while offset < len(chunk):
            state = self._state
            frame: AnyFrame | None
            if isinstance(state, _Body):
                offset, frame = self._body(state, chunk, offset)
            else:
                offset, frame = self._read_line(state, chunk, offset)
            if frame is not None:
                yield frame

    def finish(self) -> None:
        if self._state is not _CommandLine.WAITING or self._buffer:
            raise ProtocolError(reason="incomplete frame at end of stream")


def encode_frame(frame: AnyClientFrame) -> bytes:
    validate_outgoing(frame)
    return dump_frame(frame)
