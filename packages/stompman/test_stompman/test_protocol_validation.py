"""Validate protocol meaning independently of serialization round trips."""

import asyncio
from collections.abc import AsyncGenerator
from typing import cast

import pytest
from stompman.core import ConnectionSettings, Delivery, Runtime, RuntimeConfig, Server
from stompman.core.codec import encode_frame
from stompman.core.errors import (
    ConnectionConfirmationTimeout,
    ConnectionLostError,
    FailedAllConnectAttemptsError,
    HandshakeDisconnected,
    HandshakeRejected,
    MalformedHandshake,
    ProtocolError,
)
from stompman.core.frames import (
    AckFrame,
    AnyClientFrame,
    AnyServerFrame,
    ConnectedFrame,
    ConnectFrame,
    ErrorFrame,
    HeartbeatFrame,
    MessageFrame,
    ReceiptFrame,
    SendFrame,
    SubscribeFrame,
)
from stompman.core.protocol import Stomp12

from test_stompman.conftest import ScriptedBroker, ScriptedConnection, wait_until


@pytest.mark.parametrize(
    "frame",
    [
        SendFrame(headers={}),  # type: ignore[typeddict-item]
        SendFrame(headers={"destination": "q", "content-length": "4"}, body=b"abc"),
        SendFrame(headers={"destination": "q\x00other"}),
        SendFrame(headers={"destination": "\ud800"}),
        SubscribeFrame(headers={"destination": "q", "id": "s", "ack": "unknown"}),  # type: ignore[typeddict-item]
        ConnectFrame(headers={"accept-version": "1.2", "host": "localhost", "passcode": "x\ny:injected"}),
        cast("AnyClientFrame", ReceiptFrame(headers={"receipt-id": "server-command"})),
    ],
)
def test_invalid_outgoing_frames_cannot_be_encoded(frame: AnyClientFrame) -> None:
    with pytest.raises(ProtocolError):
        encode_frame(frame)


@pytest.mark.parametrize(
    "frame",
    [
        MessageFrame(headers={}, body=b"body"),  # type: ignore[typeddict-item]
        ReceiptFrame(headers={}),  # type: ignore[typeddict-item]
        ConnectedFrame(headers={"version": "1.2"}),
        cast("AnyServerFrame", SendFrame(headers={"destination": "q"})),
    ],
)
def test_session_frames_require_the_correct_command_and_headers(frame: AnyServerFrame) -> None:
    with pytest.raises(ProtocolError):
        Stomp12.incoming(frame)


def test_recommended_headers_are_optional_and_extensions_are_preserved() -> None:
    error = ErrorFrame(headers={})  # type: ignore[typeddict-item]
    Stomp12.incoming(error)
    Stomp12.incoming(ReceiptFrame(headers={"receipt-id": ""}))
    assert encode_frame(AckFrame(headers={"id": "opaque-id"})) == b"ACK\nid:opaque-id\n\n\x00"  # type: ignore[typeddict-item]
    frame = SendFrame(headers={"destination": "q"})
    frame.headers["broker-extension"] = "  literal spaces  "  # type: ignore[typeddict-unknown-key]
    assert b"broker-extension:  literal spaces  \n" in encode_frame(frame)
    assert encode_frame(AckFrame(headers={"id": ""})) == b"ACK\nid:\n\n\x00"  # type: ignore[typeddict-item]


def test_native_sessions_only_advertise_implemented_protocol_versions() -> None:
    config = RuntimeConfig(
        (Server("localhost", 61616, "guest", "guest"),), connection=ConnectionSettings(protocol_version="1.1")
    )
    with pytest.raises(ProtocolError, match=r"1\.2 only"):
        Runtime(config)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (None, ConnectionConfirmationTimeout),
        (HeartbeatFrame(), ConnectionConfirmationTimeout),
        (ErrorFrame(headers={}), HandshakeRejected),  # type: ignore[typeddict-item]
        (ConnectedFrame(headers={}), MalformedHandshake),  # type: ignore[typeddict-item]
        (ConnectedFrame(headers={"version": "1.2", "heart-beat": " 1,1"}), MalformedHandshake),
        (ConnectedFrame(headers={"version": "1.2", "heart-beat": "1,-1"}), MalformedHandshake),
        (ReceiptFrame(headers={"receipt-id": "before-connected"}), MalformedHandshake),
        (ConnectionLostError(reason="eof"), HandshakeDisconnected),
    ],
)
async def test_handshake_failures_keep_their_actual_cause(
    broker: ScriptedBroker,
    monkeypatch: pytest.MonkeyPatch,
    response: AnyServerFrame | ConnectionLostError | None,
    expected: type[object],
) -> None:
    async def write(connection: ScriptedConnection, frame: AnyClientFrame) -> None:
        connection.writes.append(frame)
        if response is not None:
            connection.incoming.put_nowait(response)

    monkeypatch.setattr(broker.connection_class, "write_frame", write)
    with pytest.raises(FailedAllConnectAttemptsError) as failure:
        await broker.runtime(connect_retry_attempts=1, connection_confirmation_timeout=0.01).start()
    assert len(failure.value.issues) == 1
    assert isinstance(failure.value.issues[0], expected)
    assert broker.current.closed


@pytest.mark.anyio
async def test_exhausted_handshake_stream_is_a_disconnect(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def frames(connection: ScriptedConnection) -> AsyncGenerator[AnyServerFrame, None]:
        yield HeartbeatFrame()

    monkeypatch.setattr(broker.connection_class, "read_frames", frames)
    with pytest.raises(FailedAllConnectAttemptsError) as failure:
        await broker.runtime(connect_retry_attempts=1).start()
    assert isinstance(failure.value.issues[0], HandshakeDisconnected)
    assert broker.current.closed


@pytest.mark.parametrize("header", ["content-length", "heart-beat"])
def test_protocol_numeric_headers_reject_values_beyond_integer_parsing_limits(header: str) -> None:
    oversized_integer = "9" * 5000
    with pytest.raises(ProtocolError):
        if header == "content-length":
            encode_frame(SendFrame(headers={"destination": "q", "content-length": oversized_integer}))
        else:
            Stomp12.connected(
                ConnectedFrame(headers={"version": "1.2", "heart-beat": f"{oversized_integer},0"}),
                ConnectionSettings(),
            )


@pytest.mark.anyio
async def test_missing_ack_is_rejected_before_delivery_or_capacity_reservation(broker: ScriptedBroker) -> None:
    delivered = asyncio.Event()
    runtime = broker.runtime()
    with pytest.raises(ExceptionGroup) as failure:
        async with runtime:

            async def handle(frame: MessageFrame) -> None:
                delivered.set()

            subscription = await runtime.subscribe("q", handle)
            broker.current.deliver(subscription.id, b"missing-ack")
            await asyncio.Future()
    assert any(isinstance(error, ProtocolError) for error in failure.value.exceptions)
    assert not delivered.is_set()
    assert broker.current.closed
    assert runtime.status.pending_messages == runtime.status.pending_bytes == 0


@pytest.mark.anyio
async def test_empty_ack_identifier_is_delivered_settled_and_releases_capacity(broker: ScriptedBroker) -> None:
    delivered = asyncio.Event()
    async with broker.runtime() as runtime:

        async def handle(frame: Delivery) -> None:
            assert frame.headers["ack"] == ""  # ruff: ignore[compare-to-empty-string]
            await frame.ack()
            delivered.set()

        subscription = await runtime.subscribe("q", handle)
        broker.current.deliver(subscription.id, b"empty-ack", ack_id="")
        await delivered.wait()
        await wait_until(lambda: any(isinstance(frame, AckFrame) for frame in broker.current.writes))
        ack = next(frame for frame in broker.current.writes if isinstance(frame, AckFrame))
        assert ack.headers["id"] == ""  # ruff: ignore[compare-to-empty-string]
        await wait_until(lambda: runtime.status.pending_messages == runtime.status.pending_bytes == 0)
        assert runtime.is_alive()


@pytest.mark.anyio
async def test_invalid_publication_does_not_reserve_a_receipt_or_write(broker: ScriptedBroker) -> None:
    async with broker.runtime() as runtime:
        previous = len(broker.current.writes)
        with pytest.raises(ProtocolError, match="content-length"):
            await runtime.send(b"binary\x00body", "q", add_content_length=False)
        assert len(broker.current.writes) == previous
        assert runtime.status.pending_receipts == 0
        assert runtime.is_alive()
