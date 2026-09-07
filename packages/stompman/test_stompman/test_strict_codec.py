"""Wire examples are specified independently of the library's encoder."""

import pytest
from hypothesis import given, strategies
from stompman.core.codec import FrameDecoder, encode_frame
from stompman.core.config import FrameLimits
from stompman.core.errors import ProtocolError
from stompman.core.frames import HeartbeatFrame, MessageFrame, ReceiptFrame, SendFrame


@pytest.mark.parametrize(
    "wire",
    [
        b"UNKNOWN\n\n\x00",
        b"MESSAGE\nx:bad\\tvalue\n\nbody\x00",
        b"MESSAGE\nx:\xff\n\nbody\x00",
        b"MESSAGE\ninvalid-header\n\nbody\x00",
        b"MESSAGE\ncontent-length:1\n\nabc\x00",
        b"MESSAGE\ncontent-length:-1\n\nbody\x00",
        b"MESSAGE\ncontent-length:invalid\n\nbody\x00",
        b"MESSAGE\ncontent-length:+1\n\na\x00",
        b"MESSAGE\ncontent-length: 1\n\na\x00",
        b"RECEIPT\nreceipt-id:r\n\nbody\x00",
        b"MESSAGE\nx:a\rb\n\nbody\x00",
        b"MESSAGE\nx:unfinished\x00",
    ],
)
@pytest.mark.parametrize("chunk_size", [1, 1024])
def test_malformed_wire_is_rejected(wire: bytes, chunk_size: int) -> None:
    decoder = FrameDecoder()
    with pytest.raises(ProtocolError):
        for offset in range(0, len(wire), chunk_size):
            list(decoder.feed(wire[offset : offset + chunk_size]))


@pytest.mark.parametrize("chunk_size", [1, 2, 7, 1024])
def test_binary_framing_escapes_and_first_header_win(chunk_size: int) -> None:
    wire = (
        b"\r\nMESSAGE\r\nx:first\\cvalue\r\nx:second\r\ncontent-length:3\r\n\r\nA\x00B\x00"
        b"\nRECEIPT\nreceipt-id:r\n\n\x00"
    )
    decoder = FrameDecoder()
    frames = [
        frame
        for offset in range(0, len(wire), chunk_size)
        for frame in decoder.feed(wire[offset : offset + chunk_size])
    ]
    assert frames == [
        HeartbeatFrame(),
        MessageFrame(headers={"x": "first:value", "content-length": "3"}, body=b"A\x00B"),  # type: ignore[typeddict-item]
        HeartbeatFrame(),
        ReceiptFrame(headers={"receipt-id": "r"}),
    ]
    decoder.finish()


def test_header_names_are_case_sensitive() -> None:
    decoder = FrameDecoder()
    frames = list(decoder.feed(b"MESSAGE\nContent-Length:3\n\nA\x00"))
    assert frames == [MessageFrame(headers={"Content-Length": "3"}, body=b"A")]  # type: ignore[typeddict-item]


@pytest.mark.parametrize("wire", [b"M", b"\r", b"MESSAGE\n", b"MESSAGE\n\nbody", b"MESSAGE\ncontent-length:2\n\na"])
def test_eof_during_frame_is_a_protocol_error(wire: bytes) -> None:
    decoder = FrameDecoder()
    list(decoder.feed(wire))
    with pytest.raises(ProtocolError, match="incomplete"):
        decoder.finish()


@pytest.mark.parametrize(
    ("limits", "wire"),
    [
        (FrameLimits(line_bytes=4), b"MESSA"),
        (FrameLimits(line_bytes=8), b"MESSAGE\nx:1234567"),
        (FrameLimits(header_count=1), b"MESSAGE\nx:a\nx:b\n"),
        (FrameLimits(header_bytes=4), b"MESSAGE\nx:ab\n"),
        (FrameLimits(body_bytes=2), b"MESSAGE\n\nabc"),
        (FrameLimits(body_bytes=2), b"MESSAGE\ncontent-length:3\n\n"),
    ],
)
def test_limits_apply_before_an_incomplete_frame_can_grow(limits: FrameLimits, wire: bytes) -> None:
    with pytest.raises(ProtocolError, match="limit"):
        list(FrameDecoder(limits).feed(wire))


def test_binary_send_requires_content_length() -> None:
    with pytest.raises(ProtocolError, match="content-length"):
        encode_frame(SendFrame(headers={"destination": "q"}, body=b"a\x00b"))
    assert encode_frame(SendFrame(headers={"destination": "q", "content-length": "3"}, body=b"a\x00b")) == (
        b"SEND\ncontent-length:3\ndestination:q\n\na\x00b\x00"
    )


@given(body=strategies.binary(max_size=4096), chunk_size=strategies.integers(min_value=1, max_value=128))
def test_binary_body_survives_arbitrary_content_and_fragmentation(body: bytes, chunk_size: int) -> None:
    wire = b"MESSAGE\ncontent-length:" + str(len(body)).encode() + b"\n\n" + body + b"\x00\n"
    decoder = FrameDecoder()
    frames = [
        frame
        for offset in range(0, len(wire), chunk_size)
        for frame in decoder.feed(wire[offset : offset + chunk_size])
    ]
    assert frames == [
        MessageFrame(headers={"content-length": str(len(body))}, body=body),  # type: ignore[typeddict-item]
        HeartbeatFrame(),
    ]
    decoder.finish()
