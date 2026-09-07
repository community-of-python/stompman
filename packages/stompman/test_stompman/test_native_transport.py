"""Exercise framing order with deterministic TCP read boundaries."""

import asyncio
from unittest.mock import create_autospec

import pytest
from stompman.core.errors import ConnectionLostError, ProtocolError
from stompman.core.frames import ErrorFrame, ReceiptFrame
from stompman.core.transport import TcpTransport

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("frame_type", [ErrorFrame, ReceiptFrame])
@pytest.mark.parametrize("resume_reader", [False, True])
async def test_valid_frame_precedes_a_decoding_failure_in_the_same_chunk(
    frame_type: type[ErrorFrame | ReceiptFrame], resume_reader: bool
) -> None:
    command = b"ERROR" if frame_type is ErrorFrame else b"RECEIPT"
    reader = asyncio.StreamReader()
    reader.feed_data(command + b"\nreceipt-id:expected\n\n\x00BOGUS\n")
    reader.feed_eof()
    transport = TcpTransport(reader, create_autospec(asyncio.StreamWriter, instance=True), 4096)
    frames = transport.read_frames()
    try:
        frame = await anext(frames)
        assert isinstance(frame, frame_type)
        assert frame.headers["receipt-id"] == "expected"
        if resume_reader:
            await frames.aclose()
            frames = transport.read_frames()
        with pytest.raises(ProtocolError, match="unknown frame command"):
            await anext(frames)
    finally:
        await frames.aclose()
        await transport.close()


async def test_transport_read_failure_preserves_the_socket_error() -> None:
    reader = asyncio.StreamReader()
    failure = ConnectionResetError("peer reset")
    reader.set_exception(failure)
    transport = TcpTransport(reader, create_autospec(asyncio.StreamWriter, instance=True), 4096)
    frames = transport.read_frames()
    try:
        with pytest.raises(ConnectionLostError) as error:
            await anext(frames)
        assert error.value.reason is failure
        assert error.value.__cause__ is failure
    finally:
        await frames.aclose()
        await transport.close()
