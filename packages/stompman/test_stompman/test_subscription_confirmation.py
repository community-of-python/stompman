import asyncio
from collections.abc import AsyncGenerator, Coroutine
from typing import Any
from unittest import mock

import pytest
import stompman
from stompman.connection import AbstractConnection

from test_stompman.conftest import BaseMockConnection, EnrichedClient, noop_message_handler

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]
Incoming = dict[int, asyncio.Queue[stompman.AnyServerFrame | stompman.ConnectionLostError]]
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
        queue = incoming.setdefault(id(connection), asyncio.Queue())
        outgoing.put_nowait((connection, frame))
        if isinstance(frame, stompman.ConnectFrame):
            queue.put_nowait(stompman.ConnectedFrame(headers={"version": "1.2", "heart-beat": "1000,1000"}))
        elif isinstance(frame, stompman.DisconnectFrame):
            queue.put_nowait(stompman.ReceiptFrame(headers={"receipt-id": frame.headers["receipt"]}))

    async def read_frames(connection: AbstractConnection) -> AsyncGenerator[stompman.AnyServerFrame, None]:
        queue = incoming.setdefault(id(connection), asyncio.Queue())
        while True:
            frame = await queue.get()
            if isinstance(frame, stompman.ConnectionLostError):
                raise frame
            yield frame

    monkeypatch.setattr(BaseMockConnection, "write_frame", write_frame)
    monkeypatch.setattr(BaseMockConnection, "read_frames", read_frames)
    async with EnrichedClient(
        connection_class=BaseMockConnection,
        connect_retry_interval=0,
        max_concurrent_handlers=1,
        on_error_frame=mock.Mock(),
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
    incoming[id(connection)].put_nowait(stompman.ReceiptFrame(headers={"receipt-id": frame.headers["receipt"]}))


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
        incoming[id(connection)].put_nowait(stompman.ReceiptFrame(headers={"receipt-id": "unrelated"}))
        incoming[id(connection)].put_nowait(
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
    incoming[id(connection)].put_nowait(
        stompman.ErrorFrame(headers={"message": "subscription rejected", "receipt-id": frame.headers["receipt"]})
    )
    with pytest.raises(stompman.SubscriptionError, match="rejected"):
        await task
    assert len(failures) == 1
    assert not client._active_subscriptions.pending_receipts
    incoming[id(connection)].put_nowait(stompman.ConnectionLostError(reason="peer closed after ERROR"))
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
    incoming[id(connection)].put_nowait(stompman.ConnectionLostError(reason="connection lost before receipt"))
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
    incoming[id(connection)].put_nowait(stompman.ErrorFrame(headers=error_headers))
    with pytest.raises(stompman.SubscriptionError, match="rejected"):
        await failed
    assert client._active_subscriptions.get_all() == [healthy]
    incoming[id(connection)].put_nowait(stompman.ConnectionLostError(reason="peer closed"))
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
    incoming[id(connection)].put_nowait(stompman.ConnectionLostError(reason="force restore"))
    restored_connection, restored = await next_subscribe(outgoing)
    if reject:
        incoming[id(restored_connection)].put_nowait(
            stompman.ErrorFrame(headers={"message": "rejected", "receipt-id": restored.headers["receipt"]})
        )
    error = await asyncio.wait_for(failure, timeout=1)
    assert error.reason == ("rejected" if reject else "timeout")
    assert not client._active_subscriptions.get_all()
    assert not client._active_subscriptions.pending_receipts


@pytest.mark.parametrize("cancel_send", [True, False])
async def test_receipts_are_processed_while_later_subscription_replay_is_blocked(
    client: stompman.Client,
    incoming: Incoming,
    outgoing: Outgoing,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cancel_send: bool,
) -> None:
    failures: list[stompman.SubscriptionError] = []
    subscriptions: list[stompman.ManualAckSubscription] = []
    for destination, timeout in (("first", 0.1), ("second", 1)):
        task = asyncio.create_task(
            client.subscribe_with_manual_ack(
                destination, noop_message_handler, receipt_timeout=timeout, on_subscription_error=failures.append
            )
        )
        connection, frame = await next_subscribe(outgoing)
        receipt(incoming, connection, frame)
        subscriptions.append(await task)

    release_write = asyncio.Event()
    write_blocked = asyncio.Event()
    write_frame = BaseMockConnection.write_frame

    async def delayed_write(connection: BaseMockConnection, frame: stompman.AnyClientFrame) -> None:
        await write_frame(connection, frame)
        if isinstance(frame, stompman.SubscribeFrame) and frame.headers["id"] == subscriptions[1].id:
            write_blocked.set()
            await release_write.wait()

    monkeypatch.setattr(BaseMockConnection, "write_frame", delayed_write)
    incoming[id(connection)].put_nowait(stompman.ConnectionLostError(reason="force replay"))
    async with asyncio.TaskGroup() as tasks:
        try:
            for subscription in subscriptions:
                restored_connection, restored = await next_subscribe(outgoing)
                assert restored.headers["id"] == subscription.id
                receipt(incoming, restored_connection, restored)
            await asyncio.wait_for(write_blocked.wait(), timeout=1)
            send = tasks.create_task(client.send(b"after replay", "test"))
            await asyncio.sleep(0.2)
            assert not failures
            assert not client._active_subscriptions.pending_receipts
            assert client._active_subscriptions.get_all() == subscriptions
            assert not send.done()
            if cancel_send:
                send.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await send
                send = tasks.create_task(client.send(b"after cancelled send", "test"))
                await asyncio.sleep(0)
                assert not send.done()
        finally:
            release_write.set()
    assert any(isinstance(frame, stompman.SendFrame) for frame in remaining_frames(outgoing))


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
async def test_invalid_receipt_timeout(client: stompman.Client, timeout: float) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        await client.subscribe_with_manual_ack("test", noop_message_handler, receipt_timeout=timeout)
    assert not client._active_subscriptions.get_all()


@pytest.mark.parametrize("start_confirmation_early", [True, False])
async def test_receipts_are_processed_when_handler_capacity_is_exhausted(
    client: stompman.Client,
    incoming: Incoming,
    outgoing: Outgoing,
    *,
    start_confirmation_early: bool,
) -> None:
    start_confirmation = asyncio.Event()
    finished = asyncio.Event()
    failures: list[stompman.SubscriptionError] = []
    active_handlers = 0
    maximum_handlers = 0

    async def handler(frame: stompman.AckableMessageFrame) -> None:
        nonlocal active_handlers, maximum_handlers
        active_handlers += 1
        maximum_handlers = max(maximum_handlers, active_handlers)
        try:
            if frame.body == b"first":
                await start_confirmation.wait()
                subscription = await client.subscribe_with_manual_ack(
                    "responses", noop_message_handler, receipt_timeout=1, on_subscription_error=failures.append
                )
                await subscription.unsubscribe()
            else:
                finished.set()
        finally:
            active_handlers -= 1

    source = await client.subscribe_with_manual_ack("source", handler, ack="auto")
    connection, source_frame = await next_subscribe(outgoing)
    incoming[id(connection)].put_nowait(
        stompman.MessageFrame(
            headers={"subscription": source_frame.headers["id"], "destination": "source", "message-id": "1"},
            body=b"first",
        )
    )
    if start_confirmation_early:
        start_confirmation.set()
        response_connection, response_frame = await next_subscribe(outgoing)
    incoming[id(connection)].put_nowait(
        stompman.MessageFrame(
            headers={"subscription": source_frame.headers["id"], "destination": "source", "message-id": "2"},
            body=b"second",
        )
    )
    if not start_confirmation_early:
        await asyncio.sleep(0)
        start_confirmation.set()
        response_connection, response_frame = await next_subscribe(outgoing)
    receipt(incoming, response_connection, response_frame)
    await asyncio.wait_for(finished.wait(), timeout=2)
    assert failures == []
    assert maximum_handlers == 1
    await source.unsubscribe()


@pytest.mark.parametrize("cancel", [True, False])
async def test_failure_after_receipt_during_write_cleans_up(
    client: stompman.Client,
    incoming: Incoming,
    outgoing: Outgoing,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cancel: bool,
) -> None:
    write_frame = BaseMockConnection.write_frame
    blocked = asyncio.Event()

    async def blocked_write(connection: BaseMockConnection, frame: stompman.AnyClientFrame) -> None:
        await write_frame(connection, frame)
        if isinstance(frame, stompman.SubscribeFrame):
            await blocked.wait()

    monkeypatch.setattr(BaseMockConnection, "write_frame", blocked_write)
    task = asyncio.create_task(client.subscribe_with_manual_ack("test", noop_message_handler, receipt_timeout=0.05))
    connection, frame = await next_subscribe(outgoing)
    receipt(incoming, connection, frame)
    await asyncio.sleep(0)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(stompman.SubscriptionError, match="timeout"):
            await task
    assert not client._active_subscriptions.get_all()
    assert not client._active_subscriptions.pending_receipts
