"""Strict incremental framing. No socket, session, or recovery decisions live here."""

from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum, auto

from .config import DEFAULT_FRAME_LIMITS, FrameLimits
from .errors import ProtocolError
from .frames import AnyClientFrame, AnyServerFrame, HeartbeatFrame
from .serde import COMMANDS_TO_FRAMES, FRAMES_WITH_BODY, dump_frame, make_frame_from_parts, parse_header
from .validation import content_length, validate_outgoing


class CommandLine(Enum):
    WAITING = auto()


@dataclass(slots=True, kw_only=True)
class Headers:
    command: bytes
    values: dict[str, str]
    count: int
    size: int


@dataclass(frozen=True, slots=True, kw_only=True)
class Body:
    command: bytes
    headers: dict[str, str]
    length: int | None


class FrameDecoder:
    def __init__(self, limits: FrameLimits = DEFAULT_FRAME_LIMITS) -> None:
        self._limits = limits
        self._buffer = bytearray()
        self._state: CommandLine | Headers | Body = CommandLine.WAITING

    @staticmethod
    def _limit(size: int, maximum: int, name: str) -> None:
        if size > maximum:
            raise ProtocolError(reason=f"{name} limit of {maximum} exceeded")

    def _line(self, state: CommandLine | Headers) -> HeartbeatFrame | None:
        line = self._buffer.removesuffix(b"\r")
        self._buffer.clear()
        if b"\r" in line:
            raise ProtocolError(reason="unescaped carriage return in frame line")
        if isinstance(state, CommandLine):
            if not line:
                return HeartbeatFrame()
            command = bytes(line)
            if command not in COMMANDS_TO_FRAMES:
                raise ProtocolError(reason="unknown frame command")
            self._state = Headers(command=command, values={}, count=0, size=0)
        elif not line:
            length = content_length(state.values)
            if length is not None:
                self._limit(length, self._limits.body_bytes, "body")
            self._state = Body(command=state.command, headers=state.values, length=length)
        else:
            state.count += 1
            self._limit(state.count, self._limits.header_count, "header count")
            header = parse_header(line, unescape=state.command not in {b"CONNECT", b"CONNECTED"})
            if header is None or not header[0]:
                raise ProtocolError(reason="malformed header or undefined escape sequence")
            name, value = header
            state.values.setdefault(name, value)
        return None

    def _read_line(self, state: CommandLine | Headers, chunk: bytes, offset: int) -> tuple[int, HeartbeatFrame | None]:
        newline = chunk.find(b"\n", offset)
        end = len(chunk) if newline < 0 else newline
        if isinstance(state, Headers):
            state.size += end - offset + (newline >= 0)
            self._limit(state.size, self._limits.header_bytes, "headers")
        self._limit(len(self._buffer) + end - offset, self._limits.line_bytes, "line")
        if chunk.find(b"\x00", offset, end) >= 0:
            raise ProtocolError(reason="NUL before frame body")
        self._buffer.extend(chunk[offset:end])
        if newline < 0:
            return end, None
        return end + 1, self._line(state)

    def _body(self, state: Body, chunk: bytes, offset: int) -> tuple[int, AnyClientFrame | AnyServerFrame | None]:
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
        self._state = CommandLine.WAITING
        return end + 1, frame

    def feed(self, chunk: bytes) -> Iterator[AnyClientFrame | AnyServerFrame]:
        offset = 0
        while offset < len(chunk):
            state = self._state
            if isinstance(state, Body):
                offset, frame = self._body(state, chunk, offset)
            else:
                offset, frame = self._read_line(state, chunk, offset)
            if frame is not None:
                yield frame

    def finish(self) -> None:
        if self._state is not CommandLine.WAITING or self._buffer:
            raise ProtocolError(reason="incomplete frame at end of stream")


def encode_frame(frame: AnyClientFrame) -> bytes:
    validate_outgoing(frame)
    return dump_frame(frame)
