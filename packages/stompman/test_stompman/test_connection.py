import asyncio
import socket
import ssl
from collections.abc import Awaitable
from typing import Any
from unittest import mock

import pytest
from stompman import (
    AnyServerFrame,
    BeginFrame,
    CommitFrame,
    ConnectedFrame,
    ConnectionLostError,
    HeartbeatFrame,
    MessageFrame,
    ReceiptFrame,
    dump_frame,
)
from stompman.connection import Connection
from stompman.serde import NEWLINE

pytestmark = pytest.mark.anyio


async def make_connection() -> Connection | None:
    return await Connection.connect(host="localhost", port=12345, timeout=2, read_max_chunk_size=1024 * 1024, ssl=None)


async def make_mocked_connection(monkeypatch: pytest.MonkeyPatch, reader: object, writer: object) -> Connection:
    monkeypatch.setattr("asyncio.open_connection", mock.AsyncMock(return_value=(reader, writer)))
    connection = await make_connection()
    assert connection
    return connection


def mock_wait_for(monkeypatch: pytest.MonkeyPatch) -> None:
    async def mock_impl(future: Awaitable[Any], timeout: int) -> object:
        return await original_wait_for(future, timeout=0)

    original_wait_for = asyncio.wait_for
    monkeypatch.setattr("asyncio.wait_for", mock_impl)


async def test_connection_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    class MockWriter:
        close = mock.Mock()
        wait_closed = mock.AsyncMock()
        write = mock.Mock()
        drain = mock.AsyncMock()

    read_bytes = [
        b"\n\n",
        b"\nC",
        b"ON",
        b"NE",
        b"CT",
        b"ED",
        b"\n",
        b"he",
        b"ar",
        b"t-",
        b"be",
        b"at",
        b":0",
        b",0",
        b"\nse",
        b"rv",
        b"er:",
        b"som",
        b"e server\nversion:1.2\n\n\x00",
    ]
    expected_frames = [
        HeartbeatFrame(),
        HeartbeatFrame(),
        HeartbeatFrame(),
        ConnectedFrame(headers={"heart-beat": "0,0", "version": "1.2", "server": "some server"}),
    ]

    class MockReader:
        read = mock.AsyncMock(side_effect=read_bytes)

    connection = await make_mocked_connection(monkeypatch, MockReader(), MockWriter())
    connection.write_heartbeat()
    await connection.write_frame(CommitFrame(headers={"transaction": "transaction"}))

    async def take_frames(count: int) -> list[AnyServerFrame]:
        frames = []
        async for frame in connection.read_frames():
            frames.append(frame)
            if len(frames) == count:
                break

        return frames

    assert await take_frames(len(expected_frames)) == expected_frames
    await connection.close()

    MockWriter.close.assert_called_once_with()
    MockWriter.wait_closed.assert_called_once_with()
    MockWriter.drain.assert_called_once_with()
    assert MockReader.read.mock_calls == [mock.call(connection.read_max_chunk_size)] * len(read_bytes)
    assert MockWriter.write.mock_calls == [mock.call(NEWLINE), mock.call(b"COMMIT\ntransaction:transaction\n\n\x00")]


@pytest.mark.parametrize("exception", [ConnectionError, ssl.SSLError])
async def test_connection_close_connection_error(monkeypatch: pytest.MonkeyPatch, exception: type[Exception]) -> None:
    class MockWriter:
        close = mock.Mock()
        wait_closed = mock.AsyncMock(side_effect=exception)

    connection = await make_mocked_connection(monkeypatch, mock.Mock(), MockWriter())
    await connection.close()


async def test_connection_write_heartbeat_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class MockWriter:
        write = mock.Mock(side_effect=RuntimeError)

    connection = await make_mocked_connection(monkeypatch, mock.Mock(), MockWriter())
    with pytest.raises(ConnectionLostError):
        connection.write_heartbeat()


@pytest.mark.parametrize("exception", [ConnectionError, ssl.SSLError])
async def test_connection_write_frame_connection_error(
    monkeypatch: pytest.MonkeyPatch, exception: type[Exception]
) -> None:
    class MockWriter:
        write = mock.Mock()
        drain = mock.AsyncMock(side_effect=exception)

    connection = await make_mocked_connection(monkeypatch, mock.Mock(), MockWriter())
    with pytest.raises(ConnectionLostError):
        await connection.write_frame(BeginFrame(headers={"transaction": ""}))


async def test_connection_write_frame_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class MockWriter:
        write = mock.Mock(side_effect=RuntimeError)
        drain = mock.AsyncMock()

    connection = await make_mocked_connection(monkeypatch, mock.Mock(), MockWriter())
    with pytest.raises(ConnectionLostError):
        await connection.write_frame(BeginFrame(headers={"transaction": ""}))


async def test_connection_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_wait_for(monkeypatch)
    assert not await make_connection()


@pytest.mark.parametrize("exception", [BrokenPipeError, socket.gaierror, ssl.SSLCertVerificationError])
async def test_connection_connect_connection_error(monkeypatch: pytest.MonkeyPatch, exception: type[Exception]) -> None:
    monkeypatch.setattr("asyncio.open_connection", mock.AsyncMock(side_effect=exception))
    assert not await make_connection()


@pytest.mark.parametrize("exception", [BrokenPipeError, ssl.SSLError])
async def test_read_frames_connection_error(monkeypatch: pytest.MonkeyPatch, exception: type[Exception]) -> None:
    connection = await make_mocked_connection(
        monkeypatch, mock.AsyncMock(read=mock.AsyncMock(side_effect=exception)), mock.AsyncMock()
    )
    with pytest.raises(ConnectionLostError):
        [frame async for frame in connection.read_frames()]


@pytest.mark.parametrize("split_message", [False, True])
async def test_read_frames_preserves_unread_input_between_iterators(
    monkeypatch: pytest.MonkeyPatch, *, split_message: bool
) -> None:
    connected = ConnectedFrame(headers={"version": "1.2", "heart-beat": "1000,1000"})
    message = MessageFrame(headers={"destination": "queue", "subscription": "sub", "message-id": "id"}, body=b"hello")
    receipt = ReceiptFrame(headers={"receipt-id": "receipt"})
    message_bytes = dump_frame(message)
    chunks = (
        [dump_frame(connected) + message_bytes[:10], message_bytes[10:] + dump_frame(receipt)]
        if split_message
        else [dump_frame(connected) + message_bytes + dump_frame(receipt)]
    )
    reader = mock.Mock(read=mock.AsyncMock(side_effect=[*chunks, b""]))
    connection = await make_mocked_connection(monkeypatch, reader, mock.AsyncMock())
    for expected_frame in (connected, message, receipt):
        iterator = connection.read_frames()
        assert await anext(iterator) == expected_frame
        await iterator.aclose()
    assert reader.read.await_count == len(chunks)


async def test_async_heartbeat_observes_drain_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    writer = mock.Mock(write=mock.Mock(), drain=mock.AsyncMock(side_effect=ConnectionError))
    connection = await make_mocked_connection(monkeypatch, mock.Mock(), writer)
    with pytest.raises(ConnectionLostError):
        await connection.send_heartbeat()
    writer.write.assert_called_once_with(NEWLINE)
