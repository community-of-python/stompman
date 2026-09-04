import asyncio
from collections.abc import AsyncGenerator, Coroutine
from typing import Any
from unittest import mock

import pytest
import stompman
from stompman.connection import AbstractConnection

from test_stompman.conftest import BaseMockConnection, EnrichedClient, noop_message_handler

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]
Incoming = dict[AbstractConnection, asyncio.Queue[stompman.AnyServerFrame | stompman.ConnectionLostError]]
Outgoing = asyncio.Queue[tuple[AbstractConnection, stompman.AnyClientFrame]]


@pytest.fixture
def incoming() -> Incoming:
    return {}


@pytest.fixture
def outgoing() -> Outgoing:
    return asyncio.Queue()


@pytest.fixture
async def client(
    monkeypatch: pytest.MonkeyPatch, incoming: Incoming, outgoing: Outgoing
) -> AsyncGenerator[stompman.Client, None]:
    async def write_frame(  # ruff: ignore[unused-async]
        connection: AbstractConnection, frame: stompman.AnyClientFrame
    ) -> None:
        queue = incoming.setdefault(connection, asyncio.Queue())
        outgoing.put_nowait((connection, frame))
        if isinstance(frame, stompman.ConnectFrame):
            queue.put_nowait(stompman.ConnectedFrame(headers={"version": "1.2", "heart-beat": "1000,1000"}))
        elif isinstance(frame, stompman.DisconnectFrame):
            queue.put_nowait(stompman.ReceiptFrame(headers={"receipt-id": frame.headers["receipt"]}))

    async def read_frames(connection: AbstractConnection) -> AsyncGenerator[stompman.AnyServerFrame, None]:
        queue = incoming.setdefault(connection, asyncio.Queue())
        while True:
            frame = await queue.get()
            if isinstance(frame, stompman.ConnectionLostError):
                raise frame
            yield frame

    monkeypatch.setattr(BaseMockConnection, "write_frame", write_frame)
    monkeypatch.setattr(BaseMockConnection, "read_frames", read_frames)
    async with EnrichedClient(
        connection_class=BaseMockConnection, connect_retry_interval=0, on_error_frame=mock.Mock()
    ) as instance:
        try:
            yield instance
        finally:
            for subscription in instance._active_subscriptions.get_all():
                await subscription.unsubscribe()


async def next_subscribe(outgoing: Outgoing) -> tuple[AbstractConnection, stompman.SubscribeFrame]:
    async with asyncio.timeout(1):
        while True:
            connection, frame = await outgoing.get()
            if isinstance(frame, stompman.SubscribeFrame):
                return connection, frame


def receipt(incoming: Incoming, connection: AbstractConnection, frame: stompman.SubscribeFrame) -> None:
    incoming[connection].put_nowait(stompman.ReceiptFrame(headers={"receipt-id": frame.headers["receipt"]}))


def remaining_frames(outgoing: Outgoing) -> list[stompman.AnyClientFrame]:
    frames: list[stompman.AnyClientFrame] = []
    while not outgoing.empty():
        frames.append(outgoing.get_nowait()[1])
    return frames


@pytest.mark.parametrize("manual_ack", [True, False])
async def test_subscribe_waits_for_matching_receipt(
    client: stompman.Client, incoming: Incoming, outgoing: Outgoing, *, manual_ack: bool
) -> None:
    subscribe: Coroutine[Any, Any, stompman.ManualAckSubscription | stompman.AutoAckSubscription]
    if manual_ack:
        subscribe = client.subscribe_with_manual_ack("test", noop_message_handler, receipt_timeout=1)
    else:
        subscribe = client.subscribe(
            "test", noop_message_handler, on_suppressed_exception=mock.Mock(), receipt_timeout=1
        )
    async with asyncio.TaskGroup() as tasks:
        task = tasks.create_task(subscribe)
        connection, frame = await next_subscribe(outgoing)
        incoming[connection].put_nowait(stompman.ReceiptFrame(headers={"receipt-id": "unrelated"}))
        incoming[connection].put_nowait(
            stompman.ErrorFrame(headers={"message": "unrelated", "receipt-id": "unrelated"})
        )
        await asyncio.sleep(0)
        assert not task.done()
        receipt(incoming, connection, frame)
    subscription = task.result()
    assert subscription.id == frame.headers["id"]
    assert not client._active_subscriptions.pending_receipts
    await subscription.unsubscribe()


@pytest.mark.parametrize("callback_raises", [True, False])
async def test_rejected_subscription_is_removed_before_callback_and_not_replayed(
    client: stompman.Client, incoming: Incoming, outgoing: Outgoing, *, callback_raises: bool
) -> None:
    failures: list[stompman.SubscriptionError] = []

    def on_failure(error: stompman.SubscriptionError) -> None:
        assert not client._active_subscriptions.contains_by_id(error.subscription_id)
        failures.append(error)
        if callback_raises:
            msg = "broken callback"
            raise RuntimeError(msg)

    task = asyncio.create_task(
        client.subscribe_with_manual_ack(
            "test", noop_message_handler, receipt_timeout=1, on_subscription_error=on_failure
        )
    )
    connection, frame = await next_subscribe(outgoing)
    incoming[connection].put_nowait(
        stompman.ErrorFrame(headers={"message": "subscription rejected", "receipt-id": frame.headers["receipt"]})
    )
    with pytest.raises(stompman.SubscriptionError, match="rejected"):
        await task
    assert len(failures) == 1
    assert not client._active_subscriptions.pending_receipts
    incoming[connection].put_nowait(stompman.ConnectionLostError(reason="peer closed after ERROR"))
    await asyncio.sleep(0)
    await client.send(b"still usable", "test")
    assert all(not isinstance(frame, stompman.SubscribeFrame) for frame in remaining_frames(outgoing))


@pytest.mark.parametrize("cancel", [True, False])
async def test_confirmation_timeout_and_cancellation_cleanup(
    client: stompman.Client, outgoing: Outgoing, *, cancel: bool
) -> None:
    task = asyncio.create_task(client.subscribe_with_manual_ack("test", noop_message_handler, receipt_timeout=0.05))
    await next_subscribe(outgoing)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(stompman.SubscriptionError, match="timeout"):
            await task
    assert not client._active_subscriptions.get_all()
    assert not client._active_subscriptions.pending_receipts
    assert any(isinstance(frame, stompman.UnsubscribeFrame) for frame in remaining_frames(outgoing))


async def test_connection_loss_fails_unconfirmed_subscription(
    client: stompman.Client, incoming: Incoming, outgoing: Outgoing
) -> None:
    task = asyncio.create_task(client.subscribe_with_manual_ack("test", noop_message_handler, receipt_timeout=1))
    connection, _ = await next_subscribe(outgoing)
    incoming[connection].put_nowait(stompman.ConnectionLostError(reason="connection lost before receipt"))
    with pytest.raises(stompman.SubscriptionError, match="connection_lost"):
        await task
    assert not client._active_subscriptions.get_all()
    assert not client._active_subscriptions.pending_receipts


@pytest.mark.parametrize("correlated", [True, False])
async def test_concurrent_subscription_rejection_does_not_remove_confirmed_subscription(
    client: stompman.Client, incoming: Incoming, outgoing: Outgoing, *, correlated: bool
) -> None:
    first = asyncio.create_task(client.subscribe_with_manual_ack("good", noop_message_handler, receipt_timeout=1))
    connection, accepted = await next_subscribe(outgoing)
    receipt(incoming, connection, accepted)
    healthy = await first
    failed = asyncio.create_task(client.subscribe_with_manual_ack("bad", noop_message_handler, receipt_timeout=1))
    connection, rejected = await next_subscribe(outgoing)
    error_headers: stompman.frames.ErrorHeaders = {"message": "rejected"}
    if correlated:
        error_headers["receipt-id"] = rejected.headers["receipt"]
    incoming[connection].put_nowait(stompman.ErrorFrame(headers=error_headers))
    with pytest.raises(stompman.SubscriptionError, match="rejected"):
        await failed
    assert client._active_subscriptions.get_all() == [healthy]
    incoming[connection].put_nowait(stompman.ConnectionLostError(reason="peer closed"))
    restored_connection, restored = await next_subscribe(outgoing)
    assert restored.headers["id"] == healthy.id
    assert restored.headers["receipt"] != accepted.headers["receipt"]
    receipt(incoming, restored_connection, restored)
    await asyncio.sleep(0)
    await healthy.unsubscribe()


@pytest.mark.parametrize("reject", [True, False])
async def test_resubscription_failure_notifies_owner_and_cleans_up(
    client: stompman.Client, incoming: Incoming, outgoing: Outgoing, *, reject: bool
) -> None:
    failure = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(
        client.subscribe_with_manual_ack(
            "test", noop_message_handler, receipt_timeout=0.05, on_subscription_error=failure.set_result
        )
    )
    connection, initial = await next_subscribe(outgoing)
    receipt(incoming, connection, initial)
    await task
    incoming[connection].put_nowait(stompman.ConnectionLostError(reason="force restore"))
    restored_connection, restored = await next_subscribe(outgoing)
    if reject:
        incoming[restored_connection].put_nowait(
            stompman.ErrorFrame(headers={"message": "rejected", "receipt-id": restored.headers["receipt"]})
        )
    error = await asyncio.wait_for(failure, timeout=1)
    assert error.reason == ("rejected" if reject else "timeout")
    assert not client._active_subscriptions.get_all()
    assert not client._active_subscriptions.pending_receipts


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
async def test_invalid_receipt_timeout(client: stompman.Client, timeout: float) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        await client.subscribe_with_manual_ack("test", noop_message_handler, receipt_timeout=timeout)
    assert not client._active_subscriptions.get_all()


async def test_cancellation_after_receipt_during_write_cleans_up(
    client: stompman.Client, incoming: Incoming, outgoing: Outgoing, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_frame = BaseMockConnection.write_frame
    blocked = asyncio.Event()

    async def blocked_write(connection: BaseMockConnection, frame: stompman.AnyClientFrame) -> None:
        await write_frame(connection, frame)
        if isinstance(frame, stompman.SubscribeFrame):
            await blocked.wait()

    monkeypatch.setattr(BaseMockConnection, "write_frame", blocked_write)
    task = asyncio.create_task(client.subscribe_with_manual_ack("test", noop_message_handler, receipt_timeout=1))
    connection, frame = await next_subscribe(outgoing)
    receipt(incoming, connection, frame)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not client._active_subscriptions.get_all()
    assert not client._active_subscriptions.pending_receipts
