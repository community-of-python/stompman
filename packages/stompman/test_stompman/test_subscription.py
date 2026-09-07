import asyncio

import pytest
import stompman
from stompman.core import Confirmed, Delivery, Unconfirmed

from test_stompman.conftest import ScriptedBroker, wait_until

pytestmark = pytest.mark.anyio


def settlements(broker: ScriptedBroker) -> list[stompman.AckFrame | stompman.NackFrame]:
    return [
        frame
        for connection in broker.connections
        for frame in connection.writes
        if isinstance(frame, (stompman.AckFrame, stompman.NackFrame))
    ]


@pytest.mark.parametrize("ack", ["auto", "client", "client-individual"])
async def test_subscribe_headers_restore_and_unsubscribe(broker: ScriptedBroker, ack: stompman.AckMode) -> None:
    headers = {"selector": "color = 'green'", "subscription-type": "ANYCAST"}
    async with broker.runtime() as runtime:
        subscription = await runtime.subscribe("q", lambda frame: asyncio.sleep(0), ack=ack, headers=headers)
        headers["selector"] = "wrong"
        before = broker.current.subscriptions[subscription.id]
        await runtime.reconnect()
        after = broker.current.subscriptions[subscription.id]
        assert after.headers["receipt"] != before.headers["receipt"]
        assert {k: v for k, v in after.headers.items() if k != "receipt"} == {
            k: v for k, v in before.headers.items() if k != "receipt"
        }
        assert before.headers["ack"] == ack
        await subscription.unsubscribe()
        await subscription.unsubscribe()
        assert not broker.current.subscriptions
        await runtime.reconnect()
        assert not broker.current.subscriptions


@pytest.mark.parametrize(
    ("ack", "success"), [(ack, success) for ack in ("auto", "client", "client-individual") for success in (True, False)]
)
async def test_legacy_auto_ack_and_suppressed_callback(
    broker: ScriptedBroker, ack: stompman.AckMode, success: bool
) -> None:
    seen: list[stompman.MessageFrame] = []
    errors: list[Exception] = []

    async def handler(frame: stompman.MessageFrame) -> None:
        seen.append(frame)
        if not success:
            msg = "handler error"
            raise ValueError(msg)

    async with broker.client() as client:
        sub = await client.subscribe(
            "q", handler, ack=ack, on_suppressed_exception=lambda error, frame: errors.append(error)
        )
        assert isinstance(sub, stompman.AutoAckSubscription)
        broker.current.deliver(sub.id, b"one", ack_id="a1")
        await wait_until(lambda: client.core.status.running_handlers == 0 and bool(seen))
        await sub.unsubscribe()
    assert type(seen[0]) is stompman.MessageFrame
    assert len(errors) == (0 if success else 1)
    frames = settlements(broker)
    if ack == "auto":
        assert frames == []
    else:
        assert len(frames) == 1
        assert isinstance(frames[0], stompman.AckFrame if success else stompman.NackFrame)


async def test_manual_delivery_identity_and_idempotent_settlement(broker: ScriptedBroker) -> None:
    seen = []

    async def handler(frame: stompman.AckableMessageFrame) -> None:
        seen.append(frame)
        await frame.ack()
        await frame.ack()
        await frame.nack()

    async with broker.client() as client:
        sub = await client.subscribe_with_manual_ack("q", handler)
        broker.current.deliver(sub.id, b"one", ack_id="a1")
        await wait_until(lambda: len(settlements(broker)) == 1)
        await sub.unsubscribe()
    assert type(seen[0]).__module__ + "." + type(seen[0]).__name__ == "stompman.subscription.AckableMessageFrame"
    assert type(sub) is stompman.ManualAckSubscription
    assert isinstance(settlements(broker)[0], stompman.AckFrame)


async def test_routing_and_missing_subscription(broker: ScriptedBroker) -> None:
    received: list[bytes] = []

    async def handler(frame: Delivery) -> None:
        received.append(frame.body)
        await frame.ack()

    async with broker.runtime() as runtime:
        sub = await runtime.subscribe("q", handler)
        broker.current.incoming.put_nowait(
            stompman.MessageFrame(
                headers={"destination": "q", "subscription": "missing", "message-id": "ignore"}, body=b"ignore"
            )
        )
        broker.current.deliver(sub.id, b"right", ack_id="a")
        await wait_until(lambda: bool(received))
    assert received == [b"right"]


@pytest.mark.parametrize("negative", [False, True])
async def test_no_settlement_after_unsubscribe_or_reconnect(broker: ScriptedBroker, negative: bool) -> None:
    deliveries: list[Delivery] = []

    async def handler(delivery: Delivery) -> None:
        deliveries.append(delivery)

    async with broker.runtime() as runtime:
        sub = await runtime.subscribe("q", handler)
        broker.current.deliver(sub.id, b"old", ack_id="old")
        await wait_until(lambda: bool(deliveries))
        await runtime.reconnect()
        await (deliveries[0].nack() if negative else deliveries[0].ack())
        broker.current.deliver(sub.id, b"new", ack_id="new")
        await wait_until(lambda: len(deliveries) == 2)
        await (deliveries[1].nack() if negative else deliveries[1].ack())
        assert len(settlements(broker)) == 1
        assert settlements(broker)[0].headers["id"] == "new"
        broker.current.deliver(sub.id, b"removed", ack_id="removed")
        await wait_until(lambda: len(deliveries) == 3)
        await sub.unsubscribe()
        await deliveries[2].ack()
        assert len(settlements(broker)) == 1


async def test_missing_ack_header_is_logged_and_not_sent(
    broker: ScriptedBroker, caplog: pytest.LogCaptureFixture
) -> None:
    async def handler(delivery: Delivery) -> None:
        await delivery.ack()

    async with broker.client().core as runtime:
        sub = await runtime.subscribe("q", handler)
        broker.current.deliver(sub.id, b"one")
        await wait_until(lambda: "no ack header" in caplog.text)
    assert settlements(broker) == []


@pytest.mark.parametrize("manual", [False, True])
async def test_unhandled_handler_exception_does_not_kill_runtime(broker: ScriptedBroker, manual: bool) -> None:
    calls = []

    async def handler(frame: stompman.MessageFrame) -> None:
        calls.append(frame.body)
        if frame.body == b"bad":
            msg = "unhandled"
            raise ValueError(msg)

    async with broker.client() as client:
        sub: stompman.ManualAckSubscription | stompman.AutoAckSubscription
        if manual:
            sub = await client.subscribe_with_manual_ack("q", handler)
        else:
            sub = await client.subscribe(
                "q",
                handler,
                on_suppressed_exception=lambda error, frame: None,
                suppressed_exception_classes=(KeyError,),
            )
        broker.current.deliver(sub.id, b"bad", ack_id="bad")
        broker.current.deliver(sub.id, b"good", ack_id="good")
        await wait_until(lambda: len(calls) == 2)
        assert client.is_alive()
        await sub.unsubscribe()


async def test_cumulative_settlement_waits_for_completed_prefix(broker: ScriptedBroker) -> None:
    release_first = asyncio.Event()
    second_finished = asyncio.Event()

    async def handler(frame: stompman.MessageFrame) -> None:
        if frame.body == b"first":
            await release_first.wait()
        else:
            second_finished.set()

    async with broker.client() as client:
        sub = await client.subscribe("q", handler, ack="client", on_suppressed_exception=lambda error, frame: None)
        broker.current.deliver(sub.id, b"first", ack_id="a1")
        broker.current.deliver(sub.id, b"second", ack_id="a2")
        await second_finished.wait()
        assert settlements(broker) == []
        release_first.set()
        await wait_until(lambda: len(settlements(broker)) == 2)
        assert [frame.headers["id"] for frame in settlements(broker)] == ["a1", "a2"]
        await sub.unsubscribe()


async def test_cumulative_mixed_nack_and_ack_are_ordered(broker: ScriptedBroker) -> None:
    deliveries: list[Delivery] = []

    async def handler(frame: Delivery) -> None:
        deliveries.append(frame)

    async with broker.runtime() as runtime:
        sub = await runtime.subscribe("q", handler, ack="client")
        for index in range(3):
            broker.current.deliver(sub.id, str(index).encode(), ack_id=str(index))
        await wait_until(lambda: len(deliveries) == 3)
        await deliveries[2].ack()
        await deliveries[1].nack()
        assert settlements(broker) == []
        await deliveries[0].ack()
        assert [type(frame) for frame in settlements(broker)] == [
            stompman.AckFrame,
            stompman.NackFrame,
            stompman.AckFrame,
        ]


async def test_legacy_normal_exit_waits_for_subscriptions(broker: ScriptedBroker) -> None:
    ready = asyncio.Event()
    holder: list[stompman.ManualAckSubscription] = []

    async def run() -> None:
        async with broker.client() as client:
            holder.append(await client.subscribe_with_manual_ack("q", lambda frame: asyncio.sleep(0)))
            ready.set()

    task = asyncio.create_task(run())
    await ready.wait()
    await asyncio.sleep(0)
    assert not task.done()
    await holder[0].unsubscribe()
    await asyncio.wait_for(task, timeout=1)


async def test_exception_exit_cancels_handlers_and_unsubscribes(broker: ScriptedBroker) -> None:
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def handler(frame: stompman.MessageFrame) -> None:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    with pytest.raises(ValueError, match="exit"):
        async with broker.client() as client:
            sub = await client.subscribe("q", handler, on_suppressed_exception=lambda error, frame: None)
            broker.current.deliver(sub.id, b"one", ack_id="one")
            await started.wait()
            msg = "exit"
            raise ValueError(msg)
    assert cancelled.is_set()
    assert not broker.current.subscriptions
    assert broker.current.closed


async def test_handler_limit_keeps_receipts_and_errors_responsive(broker: ScriptedBroker) -> None:
    release = asyncio.Event()
    errors: list[stompman.ErrorFrame] = []

    async def handler(frame: Delivery) -> None:
        await release.wait()
        await frame.ack()

    async with broker.runtime(max_concurrent_handlers=1, on_error_frame=errors.append) as runtime:
        sub = await runtime.subscribe("q", handler)
        for index in range(5):
            broker.current.deliver(sub.id, str(index).encode(), ack_id=str(index))
        await wait_until(lambda: runtime.status.pending_messages == 5)
        assert runtime.status.running_handlers == 1
        receipt = await runtime.send(b"probe", "q", confirmation=Confirmed(0.1))
        assert isinstance(receipt, stompman.ReceiptFrame)
        broker.current.incoming.put_nowait(stompman.ErrorFrame(headers={"message": "late error"}))
        await wait_until(lambda: bool(errors))
        release.set()
        await wait_until(lambda: runtime.status.pending_messages == 0)
        await wait_until(lambda: runtime.status.generation == 2)
        assert broker.connections[0].closed


@pytest.mark.parametrize(("capacity", "bytes_limit"), [(1, 1000), (10, 1)])
async def test_overload_is_bounded_and_distinct_from_connection_loss(
    broker: ScriptedBroker, capacity: int, bytes_limit: int
) -> None:
    async def handler(frame: Delivery) -> None:
        await asyncio.Future()

    runtime = broker.runtime(max_pending_messages=capacity, max_pending_bytes=bytes_limit)
    with pytest.raises(ExceptionGroup) as info:
        async with runtime:
            sub = await runtime.subscribe("q", handler)
            broker.current.deliver(sub.id, b"one", ack_id="a1")
            broker.current.deliver(sub.id, b"two", ack_id="a2")
            await asyncio.Future()
    assert any(isinstance(error, stompman.ConsumerOverloadedError) for error in info.value.exceptions)
    assert runtime.status.pending_messages == 0
    assert broker.connect_calls == 1
    assert broker.current.closed


async def test_unconfirmed_restoration_retries_after_failed_subscribe(broker: ScriptedBroker) -> None:
    received: list[bytes] = []

    async def handle(delivery: Delivery) -> None:
        received.append(delivery.body)
        await delivery.ack()

    async with broker.runtime() as runtime:
        subscription = await runtime.subscribe("q", handle, confirmation=Unconfirmed())
        broker.fail_before = lambda frame, connection: (
            isinstance(frame, stompman.SubscribeFrame) and connection is broker.connections[1]
        )
        await runtime.reconnect()
        assert len(broker.connections) == 3
        assert subscription.id in broker.current.subscriptions
        broker.current.deliver(subscription.id, b"after retry", ack_id="retry")
        await wait_until(lambda: received == [b"after retry"])
        await subscription.unsubscribe()
