import asyncio
from collections.abc import AsyncGenerator

import pytest
import stompman
from stompman._compat import LegacyTransport
from stompman.core import Confirmed, Delivery, TransactionState, Unconfirmed
from stompman.core._tasks import await_cleanup
from stompman.core.handshake import NegotiatedConnection
from stompman.core.session import Session

from test_stompman.conftest import ScriptedBroker, ScriptedConnection, wait_until

pytestmark = pytest.mark.anyio


async def start_session(broker: ScriptedBroker) -> tuple[Session, ScriptedConnection]:
    config = broker.config()
    connection = broker.connection_class(broker, "localhost")
    peer = await NegotiatedConnection.open(LegacyTransport(connection), config.servers[0], config.connection)
    session = Session(peer, config.connection, 1, lambda frame, owner: None)
    return session, connection


async def test_command_can_be_cancelled_before_its_task_starts(broker: ScriptedBroker) -> None:
    session, connection = await start_session(broker)
    try:
        command = session.command(stompman.SendFrame(headers={"destination": "q"}, body=b"cancelled"))
        command.cancel()
        with pytest.raises(asyncio.CancelledError):
            await command.complete()
        assert session.receipts.pending_count == 0
        assert not any(isinstance(frame, stompman.SendFrame) for frame in connection.writes)
        await session.write(stompman.SendFrame(headers={"destination": "q"}, body=b"still usable"))
    finally:
        await session.close()


async def test_session_closes_commands_whose_callers_have_not_waited(broker: ScriptedBroker) -> None:
    session, _ = await start_session(broker)
    broker.receipts = False
    command = session.command(stompman.SendFrame(headers={"destination": "q"}, body=b"pending"))
    await command.submit()
    assert session.receipts.pending_count == 1
    await session.close()
    with pytest.raises((asyncio.CancelledError, stompman.ConnectionLostError)):
        await command.complete()
    assert session.receipts.pending_count == 0


async def test_completion_waiter_can_start_before_submission_waiter(broker: ScriptedBroker) -> None:
    session, _ = await start_session(broker)
    try:
        command = session.command(stompman.SendFrame(headers={"destination": "q"}, body=b"complete"))
        assert isinstance(await command.complete(), stompman.ReceiptFrame)
        await command.submit()
        assert session.receipts.pending_count == 0
    finally:
        await session.close()


@pytest.mark.parametrize("ack", ["client", "client-individual"])
async def test_cancelled_ack_recovers_without_retaining_delivery(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch, ack: stompman.AckMode
) -> None:
    deliveries: asyncio.Queue[Delivery] = asyncio.Queue()
    entered_write = asyncio.Event()
    blocked_write = asyncio.Event()
    original_write = broker.connection_class.write_frame

    async with broker.runtime() as runtime:
        first = broker.current

        async def write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
            if connection is first and isinstance(frame, stompman.AckFrame):
                entered_write.set()
                await blocked_write.wait()
            await original_write(connection, frame)

        monkeypatch.setattr(broker.connection_class, "write_frame", write)
        subscription = await runtime.subscribe("q", deliveries.put, ack=ack)
        first.deliver(subscription.id, b"message", ack_id="old")
        delivery = await deliveries.get()
        task = asyncio.create_task(delivery.ack())
        await entered_write.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await wait_until(lambda: runtime.status.generation == 2 and runtime.status.pending_messages == 0)
        assert first.closed
        await delivery.ack()
        broker.current.deliver(subscription.id, b"message", ack_id="redelivered")
        await (await deliveries.get()).ack()
        assert [frame.headers["id"] for frame in broker.current.writes if isinstance(frame, stompman.AckFrame)] == [
            "redelivered"
        ]


async def test_cancelled_subscribe_does_not_leave_an_orphan_consumer(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepted = asyncio.Event()
    blocked_write = asyncio.Event()
    original_write = broker.connection_class.write_frame

    async with broker.runtime() as runtime:
        first = broker.current

        async def write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
            await original_write(connection, frame)
            if connection is first and isinstance(frame, stompman.SubscribeFrame):
                accepted.set()
                await blocked_write.wait()

        monkeypatch.setattr(broker.connection_class, "write_frame", write)
        task = asyncio.create_task(
            runtime.subscribe("q", lambda delivery: asyncio.sleep(0), ack="auto", subscription_id="cancelled")
        )
        await accepted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await wait_until(lambda: runtime.status.generation == 2)
        assert first.closed
        assert broker.current.subscriptions == {}
        assert runtime.status.pending_messages == 0


async def test_cancelled_begin_does_not_leave_a_broker_transaction(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepted = asyncio.Event()
    blocked_write = asyncio.Event()
    original_write = broker.connection_class.write_frame

    async with broker.runtime() as runtime:
        first = broker.current

        async def write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
            await original_write(connection, frame)
            if connection is first and isinstance(frame, stompman.BeginFrame):
                accepted.set()
                await blocked_write.wait()

        monkeypatch.setattr(broker.connection_class, "write_frame", write)
        transaction = runtime.begin(transaction_id="cancelled-begin")
        task = asyncio.create_task(transaction.__aenter__())
        await accepted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await wait_until(lambda: runtime.status.generation == 2)
        assert first.closed
        assert broker.current.transactions == {}
        assert transaction.state is TransactionState.NEW
        async with transaction:
            await transaction.send(b"committed", "q")
    assert [frame.body for frame in broker.committed[0]] == [b"committed"]


async def test_cancelled_transaction_send_is_rolled_back_before_replay(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    accepted = asyncio.Event()
    blocked_write = asyncio.Event()
    original_write = broker.connection_class.write_frame

    async with broker.runtime() as runtime, runtime.begin() as transaction:
        first = broker.current

        async def write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
            await original_write(connection, frame)
            if connection is first and isinstance(frame, stompman.SendFrame) and frame.body == b"cancelled":
                accepted.set()
                await blocked_write.wait()

        monkeypatch.setattr(broker.connection_class, "write_frame", write)
        await transaction.send(b"kept", "q")
        task = asyncio.create_task(transaction.send(b"cancelled", "q"))
        await accepted.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await wait_until(lambda: runtime.status.generation == 2)
        assert first.closed
        await transaction.send(b"after", "q")
        assert [frame.body for frame in broker.current.transactions[transaction.id]] == [b"kept", b"after"]
    assert [frame.body for frame in broker.committed[0]] == [b"kept", b"after"]


async def test_cancellation_waiting_for_write_lock_preserves_the_session(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered_write = asyncio.Event()
    release_write = asyncio.Event()
    original_write = broker.connection_class.write_frame
    session, connection = await start_session(broker)

    async def write(owner: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
        if isinstance(frame, stompman.SendFrame) and frame.body == b"first":
            entered_write.set()
            await release_write.wait()
        await original_write(owner, frame)

    monkeypatch.setattr(broker.connection_class, "write_frame", write)
    first = asyncio.create_task(
        session.write(stompman.SendFrame(headers={"destination": "q"}, body=b"first"), Unconfirmed())
    )
    try:
        await entered_write.wait()
        second = asyncio.create_task(
            session.write(stompman.SendFrame(headers={"destination": "q"}, body=b"second"), confirmation=Confirmed(60))
        )
        await wait_until(lambda: bool(session.receipts.pending_count))
        second.cancel()
        with pytest.raises(asyncio.CancelledError):
            await second
        assert session.is_alive()
        assert session.receipts.pending_count == 0
    finally:
        release_write.set()
        await first
        await session.close()
    assert [frame.body for frame in connection.writes if isinstance(frame, stompman.SendFrame)] == [b"first"]


async def test_receipt_wait_cancellation_does_not_repeat_send_or_poison_session(broker: ScriptedBroker) -> None:
    broker.receipts = False
    session, connection = await start_session(broker)
    task = asyncio.create_task(
        session.write(stompman.SendFrame(headers={"destination": "q"}, body=b"accepted"), confirmation=Confirmed(60))
    )
    try:
        await wait_until(lambda: bool(session.receipts.pending_count) and not session.writing)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.is_alive()
        assert session.receipts.pending_count == 0
        sent = next(frame for frame in connection.writes if isinstance(frame, stompman.SendFrame))
        connection.incoming.put_nowait(stompman.ReceiptFrame(headers={"receipt-id": sent.headers["receipt"]}))
        broker.receipts = True
        receipt = await session.write(
            stompman.SendFrame(headers={"destination": "q"}, body=b"next"), confirmation=Confirmed(1)
        )
        assert isinstance(receipt, stompman.ReceiptFrame)
    finally:
        await session.close()
    assert [frame.body for frame in connection.writes if isinstance(frame, stompman.SendFrame)] == [
        b"accepted",
        b"next",
    ]


async def test_cancelled_close_finishes_async_reader_and_transport_cleanup(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    cleanup_entered = asyncio.Event()
    cleanup_release = asyncio.Event()
    original_frames = broker.connection_class.read_frames
    original_close = broker.connection_class.close
    close_calls = 0

    async def read_frames(connection: ScriptedConnection) -> AsyncGenerator[stompman.AnyServerFrame, None]:
        try:
            async for frame in original_frames(connection):
                yield frame
        finally:
            cleanup_entered.set()
            await cleanup_release.wait()

    async def close(connection: ScriptedConnection) -> None:
        nonlocal close_calls
        close_calls += 1
        await original_close(connection)

    monkeypatch.setattr(broker.connection_class, "read_frames", read_frames)
    monkeypatch.setattr(broker.connection_class, "close", close)
    session, connection = await start_session(broker)
    task = asyncio.create_task(session.close())
    await cleanup_entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    second = asyncio.create_task(session.close())
    try:
        await asyncio.sleep(0)
        assert not task.done()
        assert not second.done()
        assert not connection.closed
    finally:
        cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        await second
    assert connection.closed
    assert close_calls == 1
    await session.close()
    assert close_calls == 1


@pytest.mark.parametrize("send_heartbeat_ms", [0, 10])
async def test_heartbeat_failure_interrupts_backpressured_write_and_recovers(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch, send_heartbeat_ms: int
) -> None:
    broker.heartbeat = "10,10"
    original_write = broker.connection_class.write_frame
    blocked_write = asyncio.Event()
    async with broker.runtime(heartbeat=stompman.Heartbeat(send_heartbeat_ms, 10)) as runtime:
        first = broker.current

        async def write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
            if connection is first and isinstance(frame, stompman.SendFrame):
                await blocked_write.wait()
            await original_write(connection, frame)

        monkeypatch.setattr(broker.connection_class, "write_frame", write)
        await asyncio.wait_for(runtime.send(b"retried", "q", confirmation=Unconfirmed(attempts=3)), timeout=1)
        assert first.closed
        assert runtime.status.generation == 2
        assert [frame.body for frame in broker.current.writes if isinstance(frame, stompman.SendFrame)] == [b"retried"]


async def test_backpressured_heartbeat_does_not_block_watchdog_or_application_write(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    broker.heartbeat = "10,10"
    entered_write = asyncio.Event()
    blocked_write = asyncio.Event()
    async with broker.runtime(heartbeat=stompman.Heartbeat(10, 10)) as runtime:
        first = broker.current

        async def heartbeat(connection: ScriptedConnection) -> None:
            if connection is first:
                entered_write.set()
                await blocked_write.wait()
            connection.write_heartbeat()

        monkeypatch.setattr(broker.connection_class, "send_heartbeat", heartbeat, raising=False)
        await entered_write.wait()
        await asyncio.wait_for(runtime.send(b"retried", "q", confirmation=Unconfirmed(attempts=3)), timeout=1)
        assert first.closed
        assert runtime.status.generation == 2
        assert [frame.body for frame in broker.current.writes if isinstance(frame, stompman.SendFrame)] == [b"retried"]


@pytest.mark.parametrize("frame_type", [stompman.ConnectFrame, stompman.DisconnectFrame])
async def test_confirmation_timeout_also_bounds_transport_write(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch, frame_type: type[stompman.AnyClientFrame]
) -> None:
    blocked_write = asyncio.Event()
    original_write = broker.connection_class.write_frame

    async def write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
        if isinstance(frame, frame_type):
            await blocked_write.wait()
        await original_write(connection, frame)

    monkeypatch.setattr(broker.connection_class, "write_frame", write)
    runtime = broker.runtime(
        connect_retry_attempts=1, connection_confirmation_timeout=0.01, disconnect_confirmation_timeout=0.01
    )
    if frame_type is stompman.ConnectFrame:
        with pytest.raises(stompman.FailedAllConnectAttemptsError):
            await asyncio.wait_for(runtime.start(), timeout=1)
    else:
        await runtime.start()
        await asyncio.wait_for(runtime.close(), timeout=1)
    assert broker.current.closed


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_cleanup_finishes_before_repeated_cancellation_propagates(*, cleanup_fails: bool) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()

    async def cleanup() -> None:
        entered.set()
        await release.wait()
        if cleanup_fails:
            msg = "cleanup failed"
            raise ValueError(msg)

    owned = asyncio.create_task(cleanup())
    waiter = asyncio.create_task(await_cleanup(owned))
    await entered.wait()
    waiter.cancel()
    await asyncio.sleep(0)
    waiter.cancel()
    try:
        await asyncio.sleep(0)
        assert not waiter.done()
        assert not owned.done()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError) as info:
            await waiter
    assert owned.done()
    assert not owned.cancelled()
    if cleanup_fails:
        assert isinstance(info.value.__cause__, ValueError)
