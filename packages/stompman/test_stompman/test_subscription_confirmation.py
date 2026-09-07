import asyncio
from collections.abc import AsyncGenerator, Coroutine
from typing import Any

import pytest
import stompman

from test_stompman.conftest import ScriptedBroker, ScriptedConnection, wait_until

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]
Outgoing = asyncio.Queue[tuple[ScriptedConnection, stompman.AnyClientFrame]]


@pytest.fixture
def outgoing() -> Outgoing:
    return asyncio.Queue()


@pytest.fixture
async def client(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch, outgoing: Outgoing
) -> AsyncGenerator[stompman.Client, None]:
    broker.receipts = False
    original_write = broker.connection_class.write_frame

    async def write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
        await original_write(connection, frame)
        outgoing.put_nowait((connection, frame))

    monkeypatch.setattr(broker.connection_class, "write_frame", write)
    async with broker.client(max_concurrent_handlers=1, on_error_frame=lambda frame: None) as instance:
        try:
            yield instance
        finally:
            await instance.core.close(cancel_handlers=True)


async def noop_message_handler(frame: stompman.MessageFrame) -> None:
    pass


async def next_subscribe(outgoing: Outgoing) -> tuple[ScriptedConnection, stompman.SubscribeFrame]:
    async with asyncio.timeout(1):
        while True:
            connection, frame = await outgoing.get()
            if isinstance(frame, stompman.SubscribeFrame):
                return connection, frame


async def write_finished(client: stompman.Client) -> None:
    await wait_until(lambda: not client.core.status.writing)


def receipt(connection: ScriptedConnection, frame: stompman.SubscribeFrame) -> None:
    connection.incoming.put_nowait(stompman.ReceiptFrame(headers={"receipt-id": frame.headers["receipt"]}))


def remaining_frames(outgoing: Outgoing) -> list[stompman.AnyClientFrame]:
    frames: list[stompman.AnyClientFrame] = []
    while not outgoing.empty():
        frames.append(outgoing.get_nowait()[1])
    return frames


def registered_ids(client: stompman.Client) -> list[str]:
    # Membership is observed synchronously to verify removal before the callback.
    return list(client.core.status.subscription_ids)


@pytest.mark.parametrize("manual_ack", [True, False])
async def test_subscribe_waits_for_matching_receipt(
    client: stompman.Client, outgoing: Outgoing, *, manual_ack: bool
) -> None:
    headers = {"receipt": "caller-owned", "selector": "colour = 'green'"}
    subscribe: Coroutine[Any, Any, stompman.ManualAckSubscription | stompman.AutoAckSubscription]
    if manual_ack:
        subscribe = client.subscribe_with_manual_ack("test", noop_message_handler, headers=headers, receipt_timeout=1)
    else:
        subscribe = client.subscribe(
            "test",
            noop_message_handler,
            headers=headers,
            on_suppressed_exception=lambda error, frame: None,
            receipt_timeout=1,
        )
    async with asyncio.TaskGroup() as tasks:
        task = tasks.create_task(subscribe)
        connection, frame = await next_subscribe(outgoing)
        assert frame.headers["receipt"] != "caller-owned"
        assert frame.headers.get("selector") == headers["selector"]
        connection.incoming.put_nowait(stompman.ReceiptFrame(headers={"receipt-id": "unrelated"}))
        connection.incoming.put_nowait(stompman.ErrorFrame(headers={"message": "unrelated", "receipt-id": "unrelated"}))
        await write_finished(client)
        await asyncio.sleep(0)
        assert not task.done()
        receipt(connection, frame)
    subscription = task.result()
    assert subscription.id == frame.headers["id"]
    assert type(subscription) is (stompman.ManualAckSubscription if manual_ack else stompman.AutoAckSubscription)
    assert headers == {"receipt": "caller-owned", "selector": "colour = 'green'"}
    await subscription.unsubscribe()


async def test_default_subscription_remains_write_only(client: stompman.Client, outgoing: Outgoing) -> None:
    subscription = await client.subscribe_with_manual_ack(
        "test", noop_message_handler, headers={"receipt": "caller-owned"}
    )
    _, frame = await next_subscribe(outgoing)
    assert frame.headers["receipt"] == "caller-owned"
    await subscription.unsubscribe()


@pytest.mark.parametrize("callback_raises", [True, False])
async def test_rejected_subscription_is_removed_before_callback_and_not_replayed(
    client: stompman.Client, outgoing: Outgoing, caplog: pytest.LogCaptureFixture, *, callback_raises: bool
) -> None:
    failures: list[stompman.SubscriptionError] = []

    def on_failure(error: stompman.SubscriptionError) -> None:
        assert error.subscription_id not in registered_ids(client)
        assert not client.core.status.pending_receipts
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
    rejection = stompman.ErrorFrame(
        headers={"message": "subscription rejected", "receipt-id": frame.headers["receipt"]}
    )
    connection.incoming.put_nowait(rejection)
    with pytest.raises(stompman.SubscriptionError, match="rejected"):
        await task
    assert len(failures) == 1
    assert failures[0].frame is rejection
    if callback_raises:
        assert "broken callback" in caplog.text
    connection.incoming.put_nowait(stompman.ConnectionLostError(reason="peer closed after ERROR"))
    await asyncio.sleep(0)
    await client.send(b"still usable", "test")
    assert not registered_ids(client)
    assert all(not isinstance(frame, stompman.SubscribeFrame) for frame in remaining_frames(outgoing))


@pytest.mark.parametrize("cancel", [True, False])
async def test_confirmation_timeout_and_cancellation_cleanup(
    client: stompman.Client, outgoing: Outgoing, *, cancel: bool
) -> None:
    failures: list[stompman.SubscriptionError] = []
    task = asyncio.create_task(
        client.subscribe_with_manual_ack(
            "test", noop_message_handler, receipt_timeout=0.05, on_subscription_error=failures.append
        )
    )
    connection, sent = await next_subscribe(outgoing)
    await write_finished(client)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert failures == []
    else:
        with pytest.raises(stompman.SubscriptionError, match="timeout"):
            await task
        assert [error.reason for error in failures] == ["timeout"]
    assert not registered_ids(client)
    assert not connection.closed
    assert client.core.is_alive()
    cleanup = [frame for frame in connection.writes if isinstance(frame, stompman.UnsubscribeFrame)]
    assert [frame.headers["id"] for frame in cleanup] == [sent.headers["id"]]
    receipt(connection, sent)
    await client.send(b"after stale receipt", "test")
    await client.core.reconnect()
    assert not registered_ids(client)
    assert all(not isinstance(frame, stompman.SubscribeFrame) for frame in remaining_frames(outgoing))


async def test_connection_loss_fails_unconfirmed_subscription(client: stompman.Client, outgoing: Outgoing) -> None:
    task = asyncio.create_task(client.subscribe_with_manual_ack("test", noop_message_handler, receipt_timeout=1))
    connection, _ = await next_subscribe(outgoing)
    connection.incoming.put_nowait(stompman.ConnectionLostError(reason="connection lost before receipt"))
    with pytest.raises(stompman.SubscriptionError, match="connection_lost"):
        await task
    assert not registered_ids(client)
    await client.send(b"after recovery", "test")
    assert connection.closed
    assert all(not isinstance(frame, stompman.SubscribeFrame) for frame in remaining_frames(outgoing))


@pytest.mark.parametrize("correlated", [True, False])
async def test_concurrent_subscription_rejection_does_not_remove_confirmed_subscription(
    client: stompman.Client, outgoing: Outgoing, *, correlated: bool
) -> None:
    first = asyncio.create_task(client.subscribe_with_manual_ack("good", noop_message_handler, receipt_timeout=1))
    connection, accepted = await next_subscribe(outgoing)
    receipt(connection, accepted)
    healthy = await first
    failed = asyncio.create_task(client.subscribe_with_manual_ack("bad", noop_message_handler, receipt_timeout=1))
    connection, rejected = await next_subscribe(outgoing)
    error_headers: stompman.frames.ErrorHeaders = {"message": "rejected"}
    if correlated:
        error_headers["receipt-id"] = rejected.headers["receipt"]
    connection.incoming.put_nowait(stompman.ErrorFrame(headers=error_headers))
    with pytest.raises(stompman.SubscriptionError, match="rejected"):
        await failed
    assert registered_ids(client) == [healthy.id]
    connection.incoming.put_nowait(stompman.ConnectionLostError(reason="peer closed"))
    restored_connection, restored = await next_subscribe(outgoing)
    assert restored.headers["id"] == healthy.id
    assert restored.headers["receipt"] != accepted.headers["receipt"]
    receipt(restored_connection, restored)
    await healthy.unsubscribe()


@pytest.mark.parametrize("reject", [True, False])
async def test_resubscription_failure_notifies_owner_and_cleans_up(
    client: stompman.Client, outgoing: Outgoing, *, reject: bool
) -> None:
    failure: asyncio.Future[stompman.SubscriptionError] = asyncio.get_running_loop().create_future()
    task = asyncio.create_task(
        client.subscribe_with_manual_ack(
            "test", noop_message_handler, receipt_timeout=0.05, on_subscription_error=failure.set_result
        )
    )
    connection, initial = await next_subscribe(outgoing)
    receipt(connection, initial)
    await task
    connection.incoming.put_nowait(stompman.ConnectionLostError(reason="force restore"))
    restored_connection, restored = await next_subscribe(outgoing)
    if reject:
        restored_connection.incoming.put_nowait(
            stompman.ErrorFrame(headers={"message": "rejected", "receipt-id": restored.headers["receipt"]})
        )
    error = await asyncio.wait_for(failure, timeout=1)
    assert error.reason == ("rejected" if reject else "timeout")
    assert not registered_ids(client)
    await client.send(b"after failed restore", "test")
    assert all(not isinstance(frame, stompman.SubscribeFrame) for frame in remaining_frames(outgoing))


@pytest.mark.parametrize("cancel_send", [True, False])
async def test_receipts_are_processed_while_later_subscription_replay_is_blocked(
    client: stompman.Client,
    broker: ScriptedBroker,
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
        receipt(connection, frame)
        subscriptions.append(await task)
    release_write = asyncio.Event()
    write_blocked = asyncio.Event()
    original_write = broker.connection_class.write_frame

    async def delayed_write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
        await original_write(connection, frame)
        if isinstance(frame, stompman.SubscribeFrame) and frame.headers["id"] == subscriptions[1].id:
            write_blocked.set()
            await release_write.wait()

    monkeypatch.setattr(broker.connection_class, "write_frame", delayed_write)
    connection.incoming.put_nowait(stompman.ConnectionLostError(reason="force replay"))
    async with asyncio.TaskGroup() as tasks:
        try:
            for subscription in subscriptions:
                restored_connection, restored = await next_subscribe(outgoing)
                assert restored.headers["id"] == subscription.id
                receipt(restored_connection, restored)
            await asyncio.wait_for(write_blocked.wait(), timeout=1)
            send = tasks.create_task(client.send(b"after replay", "test"))
            await asyncio.sleep(0.2)
            assert not failures
            assert registered_ids(client) == [subscription.id for subscription in subscriptions]
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


@pytest.mark.parametrize("connection_lost", [True, False])
async def test_failed_replay_after_receipt_notifies_owner(
    client: stompman.Client,
    broker: ScriptedBroker,
    outgoing: Outgoing,
    monkeypatch: pytest.MonkeyPatch,
    *,
    connection_lost: bool,
) -> None:
    failures: list[stompman.SubscriptionError] = []
    failed = asyncio.Event()

    def on_failure(error: stompman.SubscriptionError) -> None:
        failures.append(error)
        failed.set()

    task = asyncio.create_task(
        client.subscribe_with_manual_ack(
            "test", noop_message_handler, receipt_timeout=0.05, on_subscription_error=on_failure
        )
    )
    connection, initial = await next_subscribe(outgoing)
    receipt(connection, initial)
    await task
    original_write = broker.connection_class.write_frame

    async def blocked_write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
        await original_write(connection, frame)
        if isinstance(frame, stompman.SubscribeFrame):
            await asyncio.Future()

    monkeypatch.setattr(broker.connection_class, "write_frame", blocked_write)
    connection.incoming.put_nowait(stompman.ConnectionLostError(reason="force replay"))
    restored_connection, restored = await next_subscribe(outgoing)
    receipt(restored_connection, restored)
    if connection_lost:
        restored_connection.incoming.put_nowait(stompman.ConnectionLostError(reason="lost while draining"))
    await asyncio.wait_for(failed.wait(), timeout=1)
    assert [error.reason for error in failures] == ["connection_lost" if connection_lost else "timeout"]
    assert not registered_ids(client)
    await client.send(b"still usable", "test")
    assert all(not isinstance(frame, stompman.SubscribeFrame) for frame in remaining_frames(outgoing))


async def test_transaction_finalization_does_not_overtake_replay(
    client: stompman.Client, broker: ScriptedBroker, outgoing: Outgoing, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = asyncio.create_task(client.subscribe_with_manual_ack("test", noop_message_handler, receipt_timeout=1))
    connection, initial = await next_subscribe(outgoing)
    receipt(connection, initial)
    await task
    original_write = broker.connection_class.write_frame
    release_write = asyncio.Event()

    async def blocked_write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
        await original_write(connection, frame)
        if isinstance(frame, stompman.SubscribeFrame):
            await release_write.wait()

    monkeypatch.setattr(broker.connection_class, "write_frame", blocked_write)
    transaction = client.begin()
    await transaction.__aenter__()
    await transaction.send(b"buffered message", "test")
    remaining_frames(outgoing)
    connection.incoming.put_nowait(stompman.ConnectionLostError(reason="force replay"))
    async with asyncio.TaskGroup() as tasks:
        try:
            restored_connection, restored = await next_subscribe(outgoing)
            receipt(restored_connection, restored)
            commit = tasks.create_task(transaction.__aexit__(None, None, None))
            await asyncio.sleep(0)
            assert not commit.done()
            assert not any(isinstance(frame, stompman.CommitFrame) for frame in remaining_frames(outgoing))
        finally:
            release_write.set()
    await client.send(b"after replay", "test")
    replayed = remaining_frames(outgoing)
    assert [frame.body for frame in replayed if isinstance(frame, stompman.SendFrame)] == [
        b"buffered message",
        b"after replay",
    ]
    assert len([frame for frame in replayed if isinstance(frame, stompman.CommitFrame)]) == 1
    assert [[frame.body for frame in batch] for batch in broker.committed] == [[b"buffered message"]]


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), float("inf")])
async def test_invalid_receipt_timeout(client: stompman.Client, outgoing: Outgoing, timeout: float) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        await client.subscribe_with_manual_ack("test", noop_message_handler, receipt_timeout=timeout)
    assert not registered_ids(client)
    assert all(not isinstance(frame, stompman.SubscribeFrame) for frame in remaining_frames(outgoing))


@pytest.mark.parametrize("start_confirmation_early", [True, False])
async def test_receipts_are_processed_when_handler_capacity_is_exhausted(
    client: stompman.Client, outgoing: Outgoing, *, start_confirmation_early: bool
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
    connection.deliver(source_frame.headers["id"], b"first")
    if start_confirmation_early:
        start_confirmation.set()
        response_connection, response_frame = await next_subscribe(outgoing)
    connection.deliver(source_frame.headers["id"], b"second")
    if not start_confirmation_early:
        await asyncio.sleep(0)
        start_confirmation.set()
        response_connection, response_frame = await next_subscribe(outgoing)
    receipt(response_connection, response_frame)
    await asyncio.wait_for(finished.wait(), timeout=2)
    assert failures == []
    assert maximum_handlers == 1
    await source.unsubscribe()


@pytest.mark.parametrize("cancel", [True, False])
async def test_failure_after_receipt_during_write_cleans_up(
    client: stompman.Client,
    broker: ScriptedBroker,
    outgoing: Outgoing,
    monkeypatch: pytest.MonkeyPatch,
    *,
    cancel: bool,
) -> None:
    original_write = broker.connection_class.write_frame
    blocked = asyncio.Event()

    async def blocked_write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
        await original_write(connection, frame)
        if isinstance(frame, stompman.SubscribeFrame):
            await blocked.wait()

    monkeypatch.setattr(broker.connection_class, "write_frame", blocked_write)
    task = asyncio.create_task(client.subscribe_with_manual_ack("test", noop_message_handler, receipt_timeout=0.05))
    connection, frame = await next_subscribe(outgoing)
    receipt(connection, frame)
    await asyncio.sleep(0)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        with pytest.raises(stompman.SubscriptionError, match="timeout"):
            await task
    assert not registered_ids(client)
    await client.send(b"after failed write", "test")
    assert connection.closed
    assert all(not isinstance(frame, stompman.SubscribeFrame) for frame in remaining_frames(outgoing))


async def test_unsubscribe_pending_restore_drops_late_receipt_without_error_callback(
    client: stompman.Client, outgoing: Outgoing
) -> None:
    failures: list[stompman.SubscriptionError] = []
    task = asyncio.create_task(
        client.subscribe_with_manual_ack(
            "test", noop_message_handler, receipt_timeout=1, on_subscription_error=failures.append
        )
    )
    connection, initial = await next_subscribe(outgoing)
    receipt(connection, initial)
    subscription = await task
    connection.incoming.put_nowait(stompman.ConnectionLostError(reason="force restore"))
    restored_connection, restored = await next_subscribe(outgoing)
    await write_finished(client)
    await subscription.unsubscribe()
    receipt(restored_connection, restored)
    await client.send(b"after unsubscribe", "test")
    generation = client.core.status.generation
    restored_connection.incoming.put_nowait(stompman.ConnectionLostError(reason="test supervisor survived"))
    await wait_until(lambda: client.core.status.generation > generation and client.is_alive())
    assert failures == []
    assert not registered_ids(client)
    assert all(not isinstance(frame, stompman.SubscribeFrame) for frame in remaining_frames(outgoing))


def test_subscription_error_repr_excludes_raw_error_frame() -> None:
    frame = stompman.ErrorFrame(headers={"message": "private broker diagnostic"}, body=b"private payload")
    error = stompman.SubscriptionError(subscription_id="subscription-1", reason="rejected", frame=frame)
    assert error.frame is frame
    for rendered in (str(error), repr(error)):
        assert "subscription-1" in rendered
        assert "rejected" in rendered
        assert "private broker diagnostic" not in rendered
        assert "private payload" not in rendered
