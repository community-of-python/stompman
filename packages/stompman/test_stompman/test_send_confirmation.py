import asyncio
import math
import typing
from unittest import mock

import pytest
import stompman
from stompman.connection import AbstractConnection
from stompman.receipts import PendingReceipt, PendingReceipts, wait_for_result

from test_stompman.conftest import BaseMockConnection, Incoming, Outgoing, remaining_frames

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]


async def next_send(outgoing: Outgoing) -> tuple[AbstractConnection, stompman.SendFrame]:
    async with asyncio.timeout(1):
        while True:
            connection, frame = await outgoing.get()
            if isinstance(frame, stompman.SendFrame):
                return connection, frame


def receipt(incoming: Incoming, connection: AbstractConnection, frame: stompman.SendFrame) -> None:
    incoming[id(connection)].put_nowait(stompman.ReceiptFrame(headers={"receipt-id": frame.headers["receipt"]}))


async def test_send_without_receipt_timeout_does_not_wait_for_receipt(
    client: stompman.Client, outgoing: Outgoing
) -> None:
    await client.send(b"hi", "destination")

    _, frame = await next_send(outgoing)
    assert "receipt" not in frame.headers
    assert not client._receipts.pending


async def test_confirmed_send_waits_for_matching_receipt(
    client: stompman.Client, incoming: Incoming, outgoing: Outgoing
) -> None:
    headers = {"persistent": "true"}
    task = asyncio.create_task(client.send(b"hi", "destination", headers=headers, receipt_timeout=1))

    connection, frame = await next_send(outgoing)
    assert frame.headers["receipt"]
    assert headers == {"persistent": "true"}
    incoming[id(connection)].put_nowait(stompman.ReceiptFrame(headers={"receipt-id": "unrelated"}))
    incoming[id(connection)].put_nowait(
        stompman.ErrorFrame(headers={"message": "unrelated", "receipt-id": "unrelated"})
    )
    await asyncio.sleep(0)
    assert not task.done()

    receipt(incoming, connection, frame)
    await task
    assert not client._receipts.pending


async def test_concurrent_confirmed_sends_complete_independently(
    client: stompman.Client, incoming: Incoming, outgoing: Outgoing
) -> None:
    first = asyncio.create_task(client.send(b"first", "destination", receipt_timeout=1))
    connection, first_frame = await next_send(outgoing)
    second = asyncio.create_task(client.send(b"second", "destination", receipt_timeout=1))
    _, second_frame = await next_send(outgoing)
    assert first_frame.headers["receipt"] != second_frame.headers["receipt"]

    receipt(incoming, connection, second_frame)
    await second
    assert not first.done()

    receipt(incoming, connection, first_frame)
    await first
    assert not client._receipts.pending


async def test_correlated_error_rejects_send(client: stompman.Client, incoming: Incoming, outgoing: Outgoing) -> None:
    task = asyncio.create_task(client.send(b"hi", "destination", receipt_timeout=1))
    connection, frame = await next_send(outgoing)
    error_frame = stompman.ErrorFrame(headers={"message": "rejected", "receipt-id": frame.headers["receipt"]})
    incoming[id(connection)].put_nowait(error_frame)

    with pytest.raises(stompman.SendError) as exc_info:
        await task

    assert exc_info.value.reason == "rejected"
    assert exc_info.value.receipt_id == frame.headers["receipt"]
    assert exc_info.value.frame == error_frame
    assert "rejected" in repr(exc_info.value)
    assert "message" not in repr(exc_info.value)
    assert not client._receipts.pending
    assert typing.cast("mock.Mock", client.on_error_frame).mock_calls == [mock.call(error_frame)]


async def test_uncorrelated_error_rejects_pending_send(
    client: stompman.Client, incoming: Incoming, outgoing: Outgoing
) -> None:
    task = asyncio.create_task(client.send(b"hi", "destination", receipt_timeout=1))
    connection, _ = await next_send(outgoing)
    incoming[id(connection)].put_nowait(stompman.ErrorFrame(headers={"message": "broker is unhappy"}))

    with pytest.raises(stompman.SendError, match="rejected"):
        await task

    assert not client._receipts.pending


async def test_timeout_raises_and_removes_pending_state(client: stompman.Client, outgoing: Outgoing) -> None:
    task = asyncio.create_task(client.send(b"hi", "destination", receipt_timeout=0.05))
    await next_send(outgoing)

    with pytest.raises(stompman.SendError, match="timeout"):
        await task

    assert not client._receipts.pending


async def test_connection_loss_raises_and_removes_pending_state(
    client: stompman.Client, incoming: Incoming, outgoing: Outgoing
) -> None:
    task = asyncio.create_task(client.send(b"hi", "destination", receipt_timeout=1))
    connection, _ = await next_send(outgoing)
    incoming[id(connection)].put_nowait(stompman.ConnectionLostError(reason="connection lost before receipt"))

    with pytest.raises(stompman.SendError, match="connection_lost"):
        await task

    assert not client._receipts.pending


async def test_cancellation_removes_pending_state(client: stompman.Client, outgoing: Outgoing) -> None:
    task = asyncio.create_task(client.send(b"hi", "destination", receipt_timeout=1))
    await next_send(outgoing)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not client._receipts.pending


@pytest.mark.parametrize(("receipt_timeout", "write_duration"), [(1, 0.05), (0.05, 0.15)])
async def test_receipt_arriving_before_write_finishes_confirms_send(
    client: stompman.Client,
    incoming: Incoming,
    monkeypatch: pytest.MonkeyPatch,
    receipt_timeout: float,
    write_duration: float,
) -> None:
    write_frame = BaseMockConnection.write_frame

    async def write_then_receive_receipt(connection: BaseMockConnection, frame: stompman.AnyClientFrame) -> None:
        await write_frame(connection, frame)
        if isinstance(frame, stompman.SendFrame) and (receipt_id := frame.headers.get("receipt")):
            incoming[id(connection)].put_nowait(stompman.ReceiptFrame(headers={"receipt-id": receipt_id}))
            await asyncio.sleep(write_duration)

    monkeypatch.setattr(BaseMockConnection, "write_frame", write_then_receive_receipt)

    await client.send(b"hi", "destination", receipt_timeout=receipt_timeout)

    assert not client._receipts.pending


async def test_timeout_while_writing_raises_and_removes_pending_state(
    client: stompman.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_frame = BaseMockConnection.write_frame

    async def slow_write(connection: BaseMockConnection, frame: stompman.AnyClientFrame) -> None:
        await write_frame(connection, frame)
        if isinstance(frame, stompman.SendFrame):
            await asyncio.sleep(0.2)

    monkeypatch.setattr(BaseMockConnection, "write_frame", slow_write)

    with pytest.raises(stompman.SendError, match="timeout"):
        await client.send(b"hi", "destination", receipt_timeout=0.05)

    assert not client._receipts.pending


async def test_connection_loss_while_writing_raises_and_removes_pending_state(
    client: stompman.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_frame = BaseMockConnection.write_frame

    async def failing_write(connection: BaseMockConnection, frame: stompman.AnyClientFrame) -> None:
        if isinstance(frame, stompman.SendFrame):
            raise stompman.ConnectionLostError(reason="broken pipe")
        await write_frame(connection, frame)

    monkeypatch.setattr(BaseMockConnection, "write_frame", failing_write)

    with pytest.raises(stompman.SendError, match="connection_lost"):
        await client.send(b"hi", "destination", receipt_timeout=1)

    assert not client._receipts.pending


async def test_receipt_received_before_connection_loss_confirms_send(
    client: stompman.Client, incoming: Incoming, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_frame = BaseMockConnection.write_frame

    async def receive_receipt_then_fail(connection: BaseMockConnection, frame: stompman.AnyClientFrame) -> None:
        await write_frame(connection, frame)
        if isinstance(frame, stompman.SendFrame) and (receipt_id := frame.headers.get("receipt")):
            incoming[id(connection)].put_nowait(stompman.ReceiptFrame(headers={"receipt-id": receipt_id}))
            await asyncio.sleep(0)
            raise stompman.ConnectionLostError(reason="peer closed after receipt")

    monkeypatch.setattr(BaseMockConnection, "write_frame", receive_receipt_then_fail)

    await client.send(b"hi", "destination", receipt_timeout=1)

    assert not client._receipts.pending


async def test_cancellation_while_writing_removes_pending_state(
    client: stompman.Client, outgoing: Outgoing, monkeypatch: pytest.MonkeyPatch
) -> None:
    write_frame = BaseMockConnection.write_frame
    blocked = asyncio.Event()

    async def blocked_write(connection: BaseMockConnection, frame: stompman.AnyClientFrame) -> None:
        await write_frame(connection, frame)
        if isinstance(frame, stompman.SendFrame):
            await blocked.wait()

    monkeypatch.setattr(BaseMockConnection, "write_frame", blocked_write)
    task = asyncio.create_task(client.send(b"hi", "destination", receipt_timeout=1))
    await next_send(outgoing)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert not client._receipts.pending


async def test_receipt_delivered_at_the_deadline_confirms_send() -> None:
    loop = asyncio.get_running_loop()
    result: asyncio.Future[stompman.Error | None] = loop.create_future()
    pending = PendingReceipt(
        waiter=mock.Mock(),
        connection=mock.Mock(),
        receipt_id="receipt-id",
        epoch=0,
        deadline=loop.time() + 0.05,
        result=result,
    )
    loop.call_at(pending.deadline, result.set_result, None)

    assert await wait_for_result(pending) is None


async def test_receipt_from_previous_connection_does_not_confirm_new_send(
    client: stompman.Client, incoming: Incoming, outgoing: Outgoing
) -> None:
    lost = asyncio.create_task(client.send(b"first", "destination", receipt_timeout=1))
    old_connection, old_frame = await next_send(outgoing)
    incoming[id(old_connection)].put_nowait(stompman.ConnectionLostError(reason="peer closed"))
    with pytest.raises(stompman.SendError, match="connection_lost"):
        await lost

    task = asyncio.create_task(client.send(b"second", "destination", receipt_timeout=1))
    new_connection, new_frame = await next_send(outgoing)
    assert new_connection is not old_connection
    incoming[id(new_connection)].put_nowait(stompman.ReceiptFrame(headers={"receipt-id": old_frame.headers["receipt"]}))
    await asyncio.sleep(0)
    assert not task.done()

    receipt(incoming, new_connection, new_frame)
    await task


@pytest.mark.parametrize("error", [True, False])
def test_pending_receipts_ignore_other_epoch(*, error: bool) -> None:
    loop = asyncio.new_event_loop()
    waiter = mock.Mock()
    receipts = PendingReceipts()
    try:
        receipts.add(
            PendingReceipt(
                waiter=waiter,
                connection=mock.Mock(),
                receipt_id="receipt-id",
                epoch=0,
                deadline=loop.time(),
                result=loop.create_future(),
            )
        )

        if error:
            receipts.handle_error(stompman.ErrorFrame(headers={"message": "nope", "receipt-id": "receipt-id"}), epoch=1)
        else:
            receipts.handle_receipt(stompman.ReceiptFrame(headers={"receipt-id": "receipt-id"}), epoch=1)
    finally:
        loop.close()

    assert not waiter.mock_calls
    assert receipts.pending


@pytest.mark.parametrize("receipt_timeout", [0, -1, math.nan, math.inf])
async def test_invalid_receipt_timeout_fails_before_write(
    client: stompman.Client, outgoing: Outgoing, receipt_timeout: float
) -> None:
    with pytest.raises(ValueError, match="receipt_timeout"):
        await client.send(b"hi", "destination", receipt_timeout=receipt_timeout)

    assert not any(isinstance(frame, stompman.SendFrame) for frame in remaining_frames(outgoing))
    assert not client._receipts.pending


async def test_caller_provided_receipt_header_is_rejected(client: stompman.Client, outgoing: Outgoing) -> None:
    with pytest.raises(ValueError, match="receipt"):
        await client.send(b"hi", "destination", headers={"receipt": "mine"}, receipt_timeout=1)

    assert not any(isinstance(frame, stompman.SendFrame) for frame in remaining_frames(outgoing))
    assert not client._receipts.pending


async def test_caller_provided_receipt_header_is_kept_without_confirmation(
    client: stompman.Client, outgoing: Outgoing
) -> None:
    await client.send(b"hi", "destination", headers={"receipt": "mine"})

    _, frame = await next_send(outgoing)
    assert frame.headers["receipt"] == "mine"


async def test_confirmed_send_completes_while_message_handlers_are_saturated(
    client: stompman.Client, incoming: Incoming, outgoing: Outgoing
) -> None:
    release_handler = asyncio.Event()
    handling = asyncio.Event()

    async def handle_message(frame: stompman.AckableMessageFrame) -> None:
        handling.set()
        await release_handler.wait()

    subscription = await client.subscribe_with_manual_ack("destination", handle_message)
    connection = next(iter(incoming))
    for _ in range(2):
        incoming[connection].put_nowait(
            stompman.MessageFrame(
                headers={"destination": "destination", "message-id": "1", "subscription": subscription.id},
                body=b"hi",
            )
        )
    await asyncio.wait_for(handling.wait(), timeout=1)
    await asyncio.sleep(0)

    task = asyncio.create_task(client.send(b"hi", "destination", receipt_timeout=1))
    sending_connection, frame = await next_send(outgoing)
    receipt(incoming, sending_connection, frame)
    try:
        await task
    finally:
        release_handler.set()

    assert not client._receipts.pending
