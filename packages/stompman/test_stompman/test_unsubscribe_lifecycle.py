import asyncio
import copy
from typing import Literal

import pytest
import stompman
from stompman.core import Confirmed, Delivery

from test_stompman.conftest import ScriptedBroker, ScriptedConnection, wait_until

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("cancel_first", [False, True])
async def test_unsubscribe_callers_share_cleanup_and_reserve_id_until_it_finishes(
    broker: ScriptedBroker, cancel_first: bool
) -> None:
    received: asyncio.Queue[Delivery] = asyncio.Queue()
    async with broker.runtime() as runtime:
        subscription = await runtime.subscribe("q", received.put, subscription_id="same")
        broker.current.deliver(subscription.id, b"message", ack_id="message")
        delivery = await received.get()
        broker.receipts = False
        settling = asyncio.create_task(delivery.ack())
        await wait_until(lambda: runtime.status.pending_receipts == 1 and not runtime.status.writing)
        acknowledgement = next(frame for frame in broker.current.writes if isinstance(frame, stompman.AckFrame))
        first = asyncio.create_task(subscription.unsubscribe())
        await asyncio.sleep(0)
        second = asyncio.create_task(subscription.unsubscribe())
        await asyncio.sleep(0)
        if cancel_first:
            first.cancel()
            await asyncio.sleep(0)
        try:
            assert not first.done()
            assert not second.done()
            assert runtime.status.subscription_ids == ("same",)
            with pytest.raises(ValueError, match="subscription id is already active"):
                await runtime.subscribe("q", received.put, subscription_id="same")
        finally:
            broker.receipts = True
            broker.current.incoming.put_nowait(
                stompman.ReceiptFrame(headers={"receipt-id": acknowledgement.headers["receipt"]})
            )
            await settling
            outcomes = await asyncio.gather(first, second, return_exceptions=True)
        if cancel_first:
            assert isinstance(outcomes[0], asyncio.CancelledError)
        else:
            assert outcomes[0] is None
        assert outcomes[1] is None
        assert not runtime.status.subscription_ids
        assert "same" not in broker.current.subscriptions
        assert sum(isinstance(frame, stompman.UnsubscribeFrame) for frame in broker.current.writes) == 1
        replacement = await runtime.subscribe("q", received.put, subscription_id="same")
        assert replacement.id in broker.current.subscriptions
        await replacement.unsubscribe()


@pytest.mark.parametrize("failure", ["timeout", "rejected", "connection_lost"])
async def test_failed_unsubscribe_retires_session_before_id_can_be_reused(
    broker: ScriptedBroker,
    monkeypatch: pytest.MonkeyPatch,
    failure: Literal["timeout", "rejected", "connection_lost"],
) -> None:
    received: asyncio.Queue[Delivery] = asyncio.Queue()
    original_write = broker.connection_class.write_frame
    async with broker.runtime() as runtime:
        first_connection = broker.current

        async def write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
            if connection is first_connection and isinstance(frame, stompman.UnsubscribeFrame):
                connection.writes.append(copy.deepcopy(frame))
                if failure == "connection_lost":
                    raise stompman.ConnectionLostError(reason="connection closed during unsubscribe")
                if failure == "rejected":
                    connection.incoming.put_nowait(
                        stompman.ErrorFrame(headers={"receipt-id": frame.headers["receipt"], "message": "rejected"})
                    )
                return
            await original_write(connection, frame)

        monkeypatch.setattr(broker.connection_class, "write_frame", write)
        subscription = await runtime.subscribe(
            "q", received.put, subscription_id="same", operation_confirmation=Confirmed(0.02)
        )
        if failure == "connection_lost":
            await subscription.unsubscribe()
        else:
            error = stompman.ReceiptTimeoutError if failure == "timeout" else stompman.ReceiptRejectedError
            with pytest.raises(error):
                await subscription.unsubscribe()
        replacement = await runtime.subscribe("q", received.put, subscription_id="same")
        assert first_connection.closed
        assert runtime.status.generation == 2
        assert replacement.id in broker.current.subscriptions
        assert sum(isinstance(frame, stompman.SubscribeFrame) for frame in first_connection.writes) == 1
        assert sum(isinstance(frame, stompman.SubscribeFrame) for frame in broker.current.writes) == 1
        await replacement.unsubscribe()
