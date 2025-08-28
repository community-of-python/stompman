import struct
from collections import deque
from collections.abc import Iterator
from contextlib import suppress
from dataclasses import dataclass, field
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


@dataclass(kw_only=True, slots=True)
class FrameParser:
    _current_line: bytearray = field(default_factory=bytearray, init=False)
    _headers_processed: bool = field(default=False, init=False)
    _content_len: int = field(default=0, init=0)
    _body_len: int = field(default=0, init=0)
    _headers: dict = field(default_factory=dict)
    _command: bytes = field(default=False, init=False)
    

    def _reset(self) -> None:
        self._headers_processed = False
        self._current_line = bytearray()
        self._body_len = 0
        self._content_len = 0
        self._headers = {}
        self._command = ""

    def parse_frames_from_chunk(self, chunk: bytes) -> Iterator[AnyClientFrame | AnyServerFrame]:
        for byte in iter_bytes(chunk):
            if byte == NULL:
                if self._headers_processed:
                    # receiving body. If no content-len then stop at the first NULL byte
                    # otherelse continue reading until reachign content length
                    if self._content_len == 0 or self._body_len == self._content_len:
                        yield make_frame_from_parts(command = self._command, headers = self._headers, body = self._current_line)
                        self._reset()
                    else:
                        # update the buffer and update the bytecount
                        self._current_line += byte
                        self._body_len += 1
                else:
                    # if receiving a null while processing header reset
                    self._reset()

            elif not self._headers_processed and byte == NEWLINE:
                # processing headers here
                # when receiving a NEWLINE
                if self._current_line or self._command:
                    # if we have received a command or just received a new line
                    if self._current_line and self._current_line[-1] == CARRIAGE:
                        self._current_line.pop() # remove the extraneous final byte
                    self._headers_processed = not self._current_line  # extra empty line after headers

                    if self._current_line: # only continue if we have something
                        if not self._command: # command still empty, command comes first
                            self._command = bytes(self._current_line)
                            if self._command not in COMMANDS_TO_FRAMES:
                                self._reset()
                        else: # otherelse we need to parse headers
                            header = parse_header(self._current_line)
                            if header and header[0] not in self._headers:
                                self._headers[header[0]] = header[1]
                                if header[0] == "content-length":
                                    self._content_len = int(header[1])                            
                        # empty after processing
                        self._current_line = bytearray()
                else:
                    yield HeartbeatFrame()

            else:
                self._current_line += byte
                # update the byte count if necessary
                if self._headers_processed and self._content_len:
                    self._body_len += 1

