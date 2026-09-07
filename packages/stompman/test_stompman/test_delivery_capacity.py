import asyncio
from dataclasses import replace

import pytest
import stompman
from stompman.core import Delivery, DeliveryLimits, Paused, Runtime, Unbounded
from stompman.core.capacity import Capacity

from test_stompman.conftest import ScriptedBroker, ScriptedConnection, wait_until


@pytest.mark.parametrize("settled_first", [False, True])
def test_capacity_requires_both_completions_and_releases_only_once(settled_first: bool) -> None:
    frame = stompman.MessageFrame(headers={"destination": "q", "message-id": "one", "subscription": "s"}, body=b"one")
    capacity = Capacity(DeliveryLimits(pending_messages=1))
    reservation = capacity.reserve(frame)
    first, last = (
        (reservation.settled, reservation.handled) if settled_first else (reservation.handled, reservation.settled)
    )
    charged_bytes = capacity.pending_bytes
    first.finish()
    first.finish()
    assert capacity.pending_messages == 1
    assert capacity.pending_bytes == charged_bytes
    with pytest.raises(stompman.ConsumerOverloadedError):
        capacity.reserve(frame)

    last.finish()
    last.finish()
    assert capacity.pending_messages == 0
    assert capacity.pending_bytes == 0
    replacement = capacity.reserve(frame)
    first.finish()
    last.finish()
    assert capacity.pending_messages == 1
    assert capacity.pending_bytes == charged_bytes
    replacement.handled.finish()
    replacement.settled.finish()
    assert capacity.pending_messages == capacity.pending_bytes == 0


def test_unbounded_capacity_can_exceed_both_native_defaults() -> None:
    capacity = Capacity(DeliveryLimits(pending_messages=Unbounded(), pending_bytes=Unbounded()))
    frame = stompman.MessageFrame(
        headers={"destination": "q", "message-id": "one", "subscription": "s"}, body=bytes(64 * 1024)
    )
    reservations = [capacity.reserve(frame) for _ in range(1025)]
    assert capacity.pending_messages == 1025
    assert capacity.pending_bytes > 64 * 1024 * 1024
    for reservation in reservations:
        reservation.handled.finish()
        reservation.settled.finish()
    assert capacity.pending_messages == capacity.pending_bytes == 0


@pytest.mark.parametrize(
    "limits",
    [
        DeliveryLimits(pending_messages=1, pending_bytes=Unbounded()),
        DeliveryLimits(pending_messages=Unbounded(), pending_bytes=1),
    ],
)
def test_one_unbounded_dimension_preserves_the_other_capacity_limit(limits: DeliveryLimits) -> None:
    capacity = Capacity(limits)
    frame = stompman.MessageFrame(headers={"destination": "q", "message-id": "one", "subscription": "s"}, body=b"one")
    with pytest.raises(stompman.ConsumerOverloadedError) as info:
        capacity.reserve(frame)
        capacity.reserve(frame)
    assert info.value.max_pending_messages == limits.pending_messages
    assert info.value.max_pending_bytes == limits.pending_bytes


@pytest.mark.anyio
async def test_handler_completion_does_not_release_unsettled_capacity(broker: ScriptedBroker) -> None:
    received: list[Delivery] = []

    async def handle(delivery: Delivery) -> None:
        received.append(delivery)

    async with broker.runtime(max_pending_messages=1) as runtime:
        subscription = await runtime.subscribe("q", handle)
        broker.current.deliver(subscription.id, b"one", ack_id="one")
        await wait_until(lambda: bool(received) and not runtime.status.running_handlers)
        assert runtime.status.pending_messages == 1
        assert runtime.status.pending_bytes > 0
        await received[0].ack()
        assert runtime.status.pending_messages == runtime.status.pending_bytes == 0
        await received[0].nack()
        assert runtime.status.pending_messages == runtime.status.pending_bytes == 0


@pytest.mark.anyio
async def test_settlement_does_not_release_running_handler_capacity(broker: ScriptedBroker) -> None:
    settled = asyncio.Event()
    release = asyncio.Event()

    async def handle(delivery: Delivery) -> None:
        await delivery.ack()
        settled.set()
        await release.wait()

    async with broker.runtime(max_pending_messages=1) as runtime:
        subscription = await runtime.subscribe("q", handle)
        broker.current.deliver(subscription.id, b"one", ack_id="one")
        try:
            await settled.wait()
            assert runtime.status.running_handlers == 1
            assert runtime.status.pending_messages == 1
            assert runtime.status.pending_bytes > 0
        finally:
            release.set()
        await wait_until(lambda: not runtime.status.pending_messages)
        assert runtime.status.pending_bytes == 0


@pytest.mark.anyio
async def test_auto_ack_has_no_manual_settlement_obligation(
    broker: ScriptedBroker, caplog: pytest.LogCaptureFixture
) -> None:
    finished = asyncio.Event()

    async def handle(delivery: Delivery) -> None:
        await delivery.ack()
        await delivery.nack()
        finished.set()

    async with broker.runtime(max_pending_messages=1) as runtime:
        subscription = await runtime.subscribe("q", handle, ack="auto")
        broker.current.deliver(subscription.id, b"one")
        await finished.wait()
        await wait_until(lambda: not runtime.status.pending_messages)
        assert runtime.status.pending_bytes == 0
        assert not any(isinstance(frame, (stompman.AckFrame, stompman.NackFrame)) for frame in broker.current.writes)
        assert "no ack header" not in caplog.text


@pytest.mark.anyio
async def test_missing_ack_preserves_cumulative_order_without_holding_capacity(
    broker: ScriptedBroker, caplog: pytest.LogCaptureFixture
) -> None:
    received: list[Delivery] = []

    async def handle(delivery: Delivery) -> None:
        received.append(delivery)

    async with broker.client().core as runtime:
        subscription = await runtime.subscribe("q", handle, ack="client")
        broker.current.deliver(subscription.id, b"missing")
        broker.current.deliver(subscription.id, b"valid", ack_id="valid")
        await wait_until(lambda: len(received) == 2 and not runtime.status.running_handlers)
        await received[1].ack()
        assert runtime.status.pending_messages == 2
        assert "no ack header" not in caplog.text
        await received[0].nack()
        await received[0].ack()
        assert runtime.status.pending_messages == runtime.status.pending_bytes == 0
        acknowledgements = [frame for frame in broker.current.writes if isinstance(frame, stompman.AckFrame)]
        assert [frame.headers["id"] for frame in acknowledgements] == ["valid"]
        assert caplog.text.count("no ack header") == 1


@pytest.mark.anyio
@pytest.mark.parametrize("ack", ["client", "client-individual"])
async def test_cancelling_a_queued_settlement_leaves_the_delivery_available(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch, ack: stompman.AckMode
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    received: list[Delivery] = []
    original_write = broker.connection_class.write_frame

    async def write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
        if isinstance(frame, stompman.AckFrame) and frame.headers["id"] == "first":
            entered.set()
            await release.wait()
        await original_write(connection, frame)

    async def handle(delivery: Delivery) -> None:
        received.append(delivery)

    monkeypatch.setattr(broker.connection_class, "write_frame", write)
    async with broker.runtime() as runtime:
        subscription = await runtime.subscribe("q", handle, ack=ack)
        broker.current.deliver(subscription.id, b"first", ack_id="first")
        broker.current.deliver(subscription.id, b"second", ack_id="second")
        await wait_until(lambda: len(received) == 2 and not runtime.status.running_handlers)
        first = asyncio.create_task(received[0].ack())
        try:
            await entered.wait()
            second = asyncio.create_task(received[1].ack())
            await asyncio.sleep(0)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            assert runtime.status.pending_messages == 2
        finally:
            release.set()
            await first
        assert runtime.status.pending_messages == 1
        await received[1].ack()
        assert runtime.status.pending_messages == 0
        assert runtime.status.generation == 1
        acknowledgements = [frame for frame in broker.current.writes if isinstance(frame, stompman.AckFrame)]
        assert [frame.headers["id"] for frame in acknowledgements] == ["first", "second"]


@pytest.mark.anyio
async def test_unsubscribe_releases_queued_work_but_keeps_the_running_handler_charged(broker: ScriptedBroker) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    received: list[bytes] = []

    async def handle(delivery: Delivery) -> None:
        received.append(delivery.body)
        entered.set()
        await release.wait()
        await delivery.ack()

    async with broker.runtime(max_concurrent_handlers=1) as runtime:
        subscription = await runtime.subscribe("q", handle)
        for index in range(3):
            broker.current.deliver(subscription.id, str(index).encode(), ack_id=str(index))
        try:
            await entered.wait()
            await wait_until(lambda: runtime.status.pending_messages == 3)
            await subscription.unsubscribe()
            assert runtime.status.pending_messages == 1
            assert runtime.status.running_handlers == 1
        finally:
            release.set()
        await wait_until(lambda: not runtime.status.pending_messages)
        assert received == [b"0"]
        assert runtime.status.pending_bytes == 0
        assert not any(isinstance(frame, stompman.AckFrame) for frame in broker.current.writes)


@pytest.mark.anyio
async def test_unbounded_concurrency_starts_more_than_the_default_pending_limit(broker: ScriptedBroker) -> None:
    started = 0
    entered = asyncio.Event()
    release = asyncio.Event()
    expected = 1025

    async def handle(delivery: Delivery) -> None:
        nonlocal started
        started += 1
        if started == expected:
            entered.set()
        await release.wait()

    limits = DeliveryLimits(concurrency=Unbounded(), pending_messages=Unbounded(), pending_bytes=Unbounded())
    runtime = Runtime(replace(broker.config(), delivery=limits), transport_factory=broker.transport)
    async with runtime:
        subscription = await runtime.subscribe("q", handle, ack="auto")
        try:
            for index in range(expected):
                broker.current.deliver(subscription.id, str(index).encode())
            await entered.wait()
            assert runtime.status.running_handlers == expected
            assert runtime.status.pending_messages == expected
            assert runtime.is_alive()
        finally:
            release.set()
        await wait_until(lambda: not runtime.status.pending_messages)


@pytest.mark.anyio
async def test_paused_concurrency_preserves_receipts_and_releases_queued_work(broker: ScriptedBroker) -> None:
    received: list[bytes] = []

    async def handle(delivery: Delivery) -> None:
        received.append(delivery.body)

    runtime = Runtime(
        replace(broker.config(), delivery=DeliveryLimits(concurrency=Paused())),
        transport_factory=broker.transport,
    )
    async with runtime:
        subscription = await runtime.subscribe("q", handle)
        for index in range(3):
            broker.current.deliver(subscription.id, str(index).encode(), ack_id=str(index))
        await wait_until(lambda: runtime.status.pending_messages == 3)
        assert runtime.status.running_handlers == 0
        assert isinstance(await runtime.send(b"probe", "q"), stompman.ReceiptFrame)
        await subscription.unsubscribe()
        assert runtime.status.pending_messages == runtime.status.pending_bytes == 0
        assert received == []
