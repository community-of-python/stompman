import asyncio
import socket
from collections.abc import Awaitable
from ssl import CERT_REQUIRED, SSLContext, create_default_context
from typing import Any, Literal
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
from stompman.serde import NEWLINE

pytest.importorskip("stompman.connection_ws")
from stompman.connection_ws import WebSocketConnection
from websockets.exceptions import WebSocketException  # type: ignore[import-not-found,unused-ignore]

pytestmark = pytest.mark.anyio


async def make_connection_ws() -> WebSocketConnection | None:
    return await WebSocketConnection.connect(
        host="localhost", port=12345, ws_uri_path="/socket", timeout=2, read_max_chunk_size=1024 * 1024, ssl=None
    )


async def make_mocked_connection(monkeypatch: pytest.MonkeyPatch, connection_mock: object) -> WebSocketConnection:
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
    websocket = mock.Mock(recv=mock.AsyncMock(side_effect=chunks))
    connection = await make_mocked_connection(monkeypatch, websocket)
    for expected_frame in (connected, message, receipt):
        iterator = connection.read_frames()
        assert await anext(iterator) == expected_frame
        await iterator.aclose()
    assert websocket.recv.await_count == len(chunks)


@pytest.mark.parametrize("tls_mode", ["none", "default", "custom"])
async def test_connect_uses_matching_websocket_tls_options(monkeypatch: pytest.MonkeyPatch, tls_mode: str) -> None:
    ssl: Literal[True] | SSLContext | None = {"none": None, "default": True, "custom": create_default_context()}[
        tls_mode
    ]
    connect = mock.AsyncMock()
    monkeypatch.setattr("websockets.connect", connect)
    max_size = 4096
    connection = await WebSocketConnection.connect(
        host="broker.example", port=61614, timeout=2, read_max_chunk_size=max_size, ssl=ssl, ws_uri_path="/stomp"
    )
    assert connection is not None
    kwargs = connect.call_args.kwargs
    assert kwargs["uri"] == f"{'ws' if ssl is None else 'wss'}://broker.example:61614/stomp"
    assert kwargs["max_size"] == max_size
    if ssl is True:
        assert isinstance(kwargs["ssl"], SSLContext)
        assert kwargs["ssl"].check_hostname
        assert kwargs["ssl"].verify_mode == CERT_REQUIRED
    else:
        assert kwargs["ssl"] is ssl


async def test_async_heartbeat_observes_send_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    websocket = mock.Mock(send=mock.AsyncMock(side_effect=WebSocketException))
    connection = await make_mocked_connection(monkeypatch, websocket)
    with pytest.raises(ConnectionLostError):
        await connection.send_heartbeat()
