"""The original tolerant parser, backed by canonical core wire primitives."""

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum, auto
from typing import final

from .core import codec as _codec
from .core.codec import (
    BACKSLASH,
    CARRIAGE,
    COLON_,
    COMMANDS_TO_FRAMES,
    FRAMES_TO_COMMANDS,
    FRAMES_WITH_BODY,
    HEADER_ESCAPE_CHARS,
    HEADER_UNESCAPE_CHARS,
    NEWLINE,
    NULL,
    _unescape_header_part,
)
from .core.frames import AnyClientFrame, AnyRealServerFrame, AnyServerFrame, HeartbeatFrame

_NULL_BYTE = ord(NULL)
_NEWLINE_BYTE = ord(NEWLINE)


def iter_bytes(bytes_: bytes | bytearray) -> tuple[bytes, ...]:
    return _codec.iter_bytes(bytes_)


def dump_header(key: str, value: str) -> bytes:
    return _codec.dump_header(key, value)


def dump_frame(frame: AnyClientFrame | AnyRealServerFrame) -> bytes:
    return _codec.dump_frame(frame)


def unescape_byte(*, byte: bytes, previous_byte: bytes | None) -> bytes | None:
    if previous_byte == BACKSLASH:
        return HEADER_UNESCAPE_CHARS.get(byte)
    return None if byte == BACKSLASH else byte


def parse_header(buffer: bytearray, *, unescape: bool = True) -> tuple[str, str] | None:
    return _codec.parse_header(buffer, unescape=unescape)


def make_frame_from_parts(*, command: bytes, headers: dict[str, str], body: bytes) -> AnyClientFrame | AnyServerFrame:
    return _codec.make_frame_from_parts(command=command, headers=headers, body=body)


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
    for name, value in headers.items():
        if name.lower() == "content-length":
            try:
                candidate = int(value)
            except ValueError:
                continue
            if candidate >= 0:
                content_length = candidate
    return content_length


@dataclass(kw_only=True, slots=True, init=False)
class FrameParser:
    """Parse frames with the historical permissive behavior."""

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
                command=state.command,
                headers=state.headers,
                content_length=_body_length(state.headers),
            )
        else:
            header = parse_header(line, unescape=state.command not in {b"CONNECT", b"CONNECTED"})
            if header is not None:
                name, value = header
                state.headers.setdefault(name, value)
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


__all__ = [
    "BACKSLASH",
    "CARRIAGE",
    "COLON_",
    "COMMANDS_TO_FRAMES",
    "FRAMES_TO_COMMANDS",
    "FRAMES_WITH_BODY",
    "HEADER_ESCAPE_CHARS",
    "HEADER_UNESCAPE_CHARS",
    "NEWLINE",
    "NULL",
    "FrameParser",
    "_unescape_header_part",
    "dump_frame",
    "dump_header",
    "iter_bytes",
    "make_frame_from_parts",
    "parse_header",
    "unescape_byte",
]
