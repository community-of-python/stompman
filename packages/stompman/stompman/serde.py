import struct
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass
from typing import Any, Final, cast

from stompman.frames import (
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
BACKSLASH = b"\\"
COLON_ = b":"

HEADER_ESCAPE_CHARS: Final = {
    NEWLINE.decode(): "\\n",
    COLON_.decode(): "\\c",
    BACKSLASH.decode(): "\\\\",
    CARRIAGE.decode(): "",  # [\r]\n is newline, therefore can't be used in header
}
HEADER_UNESCAPE_CHARS: Final = {
    b"n": NEWLINE,
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
        if isinstance(frame, ConnectFrame)
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


def parse_header(buffer: bytearray) -> tuple[str, str] | None:
    key_buffer = bytearray()
    value_buffer = bytearray()
    key_parsed = False

    previous_byte = None
    just_escaped_line = False

    for byte in iter_bytes(buffer):
        if byte == COLON_:
            if key_parsed:
                return None
            key_parsed = True
        elif just_escaped_line:
            just_escaped_line = False
            if byte != BACKSLASH:
                (value_buffer if key_parsed else key_buffer).extend(byte)
        elif unescaped_byte := unescape_byte(byte=byte, previous_byte=previous_byte):
            just_escaped_line = True
            (value_buffer if key_parsed else key_buffer).extend(unescaped_byte)

        previous_byte = byte

    if key_parsed:
        with suppress(UnicodeDecodeError):
            return key_buffer.decode(), value_buffer.decode()

    return None


def make_frame_from_parts(*, command: bytes, headers: dict[str, str], body: bytes) -> AnyClientFrame | AnyServerFrame:
    frame_type = COMMANDS_TO_FRAMES[command]
    headers_ = cast("Any", headers)
    return frame_type(headers=headers_, body=body) if frame_type in FRAMES_WITH_BODY else frame_type(headers=headers_)  # type: ignore[call-arg]


@dataclass(kw_only=True, slots=True, init=False)
class FrameParser:
    _current_buf: bytearray
    _previous_byte: bytes | None
    _headers_processed: bool
    _command: bytes | None
    _headers: dict[str, str]
    _content_length: int | None

    def __init__(self) -> None:
        self._previous_byte = None
        self._reset()

    def _reset(self) -> None:
        self._current_buf = bytearray()
        self._headers_processed = False
        self._command = None
        self._headers = {}
        self._content_length = None

    def _handle_null_byte(self) -> Iterator[AnyClientFrame | AnyServerFrame]:
        if not self._command or not self._headers_processed:
            self._reset()
            return
        if self._content_length is not None and self._content_length != len(self._current_buf):
            self._current_buf += NULL
            return
        yield make_frame_from_parts(command=self._command, headers=self._headers, body=bytes(self._current_buf))
        self._reset()

    def _handle_newline_byte(self) -> Iterator[HeartbeatFrame]:
        if not self._current_buf and not self._command:
            yield HeartbeatFrame()
            return
        if self._previous_byte == CARRIAGE:
            self._current_buf.pop()
        self._headers_processed = not self._current_buf  # extra empty line after headers

        if self._command:
            self._process_header()
        else:
            self._process_command()

    def _process_command(self) -> None:
        current_buf_bytes = bytes(self._current_buf)
        if current_buf_bytes not in COMMANDS_TO_FRAMES:
            self._reset()
        else:
            self._command = current_buf_bytes
            self._current_buf = bytearray()

    def _process_header(self) -> None:
        header = parse_header(self._current_buf)
        if not header:
            self._current_buf = bytearray()
            return
        header_key, header_value = header
        if header_key not in self._headers:
            self._headers[header_key] = header_value
            if header_key.lower() == "content-length":
                with suppress(ValueError):
                    self._content_length = int(header_value)
        self._current_buf = bytearray()

    def _handle_body_byte(self, byte: bytes) -> None:
        if self._content_length is None or self._content_length != len(self._current_buf):
            self._current_buf += byte

    def parse_frames_from_chunk(self, chunk: bytes) -> Iterator[AnyClientFrame | AnyServerFrame]:
        for byte in iter_bytes(chunk):
            if byte == NULL:
                yield from self._handle_null_byte()
            elif self._headers_processed:
                self._handle_body_byte(byte)
            elif byte == NEWLINE:
                yield from self._handle_newline_byte()
            else:
                self._current_buf += byte

            self._previous_byte = byte
