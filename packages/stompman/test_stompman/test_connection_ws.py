import asyncio
import socket
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
)
from stompman.serde import NEWLINE

pytestmark = pytest.mark.anyio

WSConnection = pytest.importorskip("stompman.connection_ws")
WebSocketConnection = WSConnection.WebSocketConnection
WebSocketException = WSConnection.WebSocketException


async def make_connection_ws() -> WebSocketConnection | None:
    return await WebSocketConnection.connect(
        host="localhost", port=12345, ws_uri_path="/socket", timeout=2, read_max_chunk_size=1024 * 1024, ssl=None
    )


async def make_mocked_connection(monkeypatch: pytest.MonkeyPatch, connection_mock: object) -> WebSocketConnection:
    # websocket connect class is awaitable that returns self, but AsyncMock
    # class cannot do this, so wrap it in an asyncio.Future
    fut = asyncio.Future()
    fut.set_result(connection_mock)
    monkeypatch.setattr("websockets.connect", mock.AsyncMock(return_value=connection_mock))
    connection = await make_connection_ws()
    assert connection
    return connection


def mock_wait_for(monkeypatch: pytest.MonkeyPatch) -> None:
    async def mock_impl(future: Awaitable[Any], timeout: int) -> object:
        return await original_wait_for(future, timeout=0)

    original_wait_for = asyncio.wait_for
    monkeypatch.setattr("asyncio.wait_for", mock_impl)


async def test_connection_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
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
        recv = mock.AsyncMock(side_effect=read_bytes)
        recv_streaming = mock.AsyncMock(side_effect=read_bytes)
        close = mock.AsyncMock()
        send = mock.AsyncMock()

    connection = await make_mocked_connection(monkeypatch, MockReader())
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

    MockReader.close.assert_called_once_with()
    assert MockReader.recv.mock_calls == [mock.call(decode=False)] * len(read_bytes)
    assert MockReader.send.mock_calls == [
        mock.call(NEWLINE, text=True),
        mock.call(b"COMMIT\ntransaction:transaction\n\n\x00", text=True),
    ]


async def test_connection_close_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class MockWriter:
        send = mock.AsyncMock()
        close = mock.AsyncMock(side_effect=WebSocketException)

    connection = await make_mocked_connection(monkeypatch, MockWriter())
    await connection.close()


async def test_connection_write_heartbeat_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class MockWriter:
        send = mock.Mock(side_effect=RuntimeError)
        close = mock.AsyncMock()

    connection = await make_mocked_connection(monkeypatch, MockWriter())
    with pytest.raises(ConnectionLostError):
        connection.write_heartbeat()


async def test_connection_write_frame_connection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class MockWriter:
        send = mock.AsyncMock(side_effect=WebSocketException)
        close = mock.AsyncMock()

    connection = await make_mocked_connection(monkeypatch, MockWriter())
    with pytest.raises(ConnectionLostError):
        await connection.write_frame(BeginFrame(headers={"transaction": ""}))


async def test_connection_write_frame_runtime_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class MockWriter:
        send = mock.Mock(side_effect=RuntimeError)
        close = mock.AsyncMock()

    connection = await make_mocked_connection(monkeypatch, MockWriter())
    with pytest.raises(ConnectionLostError):
        await connection.write_frame(BeginFrame(headers={"transaction": ""}))


async def test_connection_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    mock_wait_for(monkeypatch)
    assert not await make_connection_ws()


@pytest.mark.parametrize("exception", [WebSocketException, BrokenPipeError, socket.gaierror])
async def test_connection_connect_connection_error(monkeypatch: pytest.MonkeyPatch, exception: type[Exception]) -> None:
    monkeypatch.setattr("websockets.connect", mock.AsyncMock(side_effect=exception))
    assert not await make_connection_ws()


@pytest.mark.parametrize("exception", [WebSocketException, BrokenPipeError, socket.gaierror])
async def test_read_frames_connection_error(monkeypatch: pytest.MonkeyPatch, exception: type[Exception]) -> None:
    connection = await make_mocked_connection(monkeypatch, mock.AsyncMock(recv=mock.AsyncMock(side_effect=exception)))
    with pytest.raises(ConnectionLostError):
        _ = [frame async for frame in connection.read_frames()]
