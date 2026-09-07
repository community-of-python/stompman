import asyncio
import copy
from typing import Literal

import pytest
import stompman
from stompman.core import Confirmed, Delivery

from test_stompman.conftest import ScriptedBroker, ScriptedConnection, wait_until

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("failure", ["timeout", "rejected", "cancelled"])
@pytest.mark.parametrize("accepted", [True, False])
async def test_interrupted_cumulative_prefix_retires_later_decisions_without_replaying(
    broker: ScriptedBroker,
    monkeypatch: pytest.MonkeyPatch,
    failure: Literal["timeout", "rejected", "cancelled"],
    accepted: bool,
) -> None:
    received: asyncio.Queue[Delivery] = asyncio.Queue()
    entered = asyncio.Event()
    original_write = broker.connection_class.write_frame

    async with broker.runtime(max_pending_messages=2) as runtime:
        first_connection = broker.current

        async def write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
            if connection is first_connection and isinstance(frame, (stompman.AckFrame, stompman.NackFrame)):
                connection.writes.append(copy.deepcopy(frame))
                entered.set()
                if failure == "rejected":
                    connection.incoming.put_nowait(
                        stompman.ErrorFrame(headers={"receipt-id": frame.headers["receipt"], "message": "rejected"})
                    )
                return
            await original_write(connection, frame)

        monkeypatch.setattr(broker.connection_class, "write_frame", write)
        subscription = await runtime.subscribe(
            "q", received.put, ack="client", operation_confirmation=Confirmed(0.02 if failure == "timeout" else 1)
        )
        first_connection.deliver(subscription.id, b"first", ack_id="first")
        first_connection.deliver(subscription.id, b"second", ack_id="second")
        first, second = await received.get(), await received.get()
        await wait_until(lambda: not runtime.status.running_handlers)
        await (second.nack() if accepted else second.ack())
        settling = asyncio.create_task(first.ack() if accepted else first.nack())
        await entered.wait()
        if failure == "cancelled":
            await wait_until(lambda: not runtime.status.writing)
            settling.cancel()
        error = {
            "timeout": stompman.ReceiptTimeoutError,
            "rejected": stompman.ReceiptRejectedError,
            "cancelled": asyncio.CancelledError,
        }[failure]
        with pytest.raises(error):
            await settling
        await wait_until(lambda: runtime.status.generation == 2 and runtime.status.pending_messages == 0)
        assert first_connection.closed
        settlements = [
            frame for frame in first_connection.writes if isinstance(frame, (stompman.AckFrame, stompman.NackFrame))
        ]
        assert [frame.headers["id"] for frame in settlements] == ["first"]
        assert isinstance(settlements[0], stompman.AckFrame if accepted else stompman.NackFrame)
        await first.ack()
        await second.nack()
        assert not any(isinstance(frame, (stompman.AckFrame, stompman.NackFrame)) for frame in broker.current.writes)
        broker.current.deliver(subscription.id, b"redelivered", ack_id="new")
        await (await received.get()).ack()
        await wait_until(lambda: not runtime.status.pending_messages)
        assert [frame.headers["id"] for frame in broker.current.writes if isinstance(frame, stompman.AckFrame)] == [
            "new"
        ]
