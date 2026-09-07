import asyncio
import time
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest
from stompman.core.config import Confirmed, ConnectionSettings, Heartbeat, Unconfirmed
from stompman.core.errors import BrokerError, ConnectionLostError, ReceiptRejectedError
from stompman.core.frames import AnyClientFrame, AnyServerFrame, DisconnectFrame, ErrorFrame, ReceiptFrame, SendFrame
from stompman.core.handshake import NegotiatedConnection
from stompman.core.session import Session

from test_stompman.conftest import wait_until

pytestmark = pytest.mark.anyio
NO_HEARTBEAT = Heartbeat(0, 0)


class Peer:
    def __init__(self) -> None:
        self.incoming: asyncio.Queue[AnyServerFrame] = asyncio.Queue()
        self.outgoing: asyncio.Queue[AnyClientFrame] = asyncio.Queue()
        self.drain = asyncio.Event()
        self.drain.set()
        self.last_received_at = time.monotonic()
        self.closed = False
        self.heartbeats = 0

    async def read_frames(self) -> AsyncGenerator[AnyServerFrame, None]:
        while True:
            yield await self.incoming.get()

    async def write_frame(self, frame: AnyClientFrame) -> None:
        self.outgoing.put_nowait(frame)
        await self.drain.wait()

    async def send_heartbeat(self) -> None:
        self.heartbeats += 1

    async def close(self) -> None:
        self.closed = True


@asynccontextmanager
async def connection(
    heartbeat: Heartbeat = NO_HEARTBEAT,
    *,
    receive: Callable[[AnyServerFrame, Session], None] = lambda *_: None,
) -> AsyncIterator[tuple[Peer, Session]]:
    peer = Peer()
    session = Session(
        NegotiatedConnection(peer, peer.read_frames(), heartbeat),
        ConnectionSettings(heartbeat=heartbeat, disconnect=Confirmed(1)),
        1,
        receive,
    )
    try:
        yield peer, session
    finally:
        await session.close()


@pytest.mark.parametrize("correlation", ["matched", "unrelated", "absent"])
async def test_error_closes_session_and_preserves_command_outcomes(correlation: str) -> None:
    async with connection() as (peer, session):
        commands = [asyncio.create_task(session.write(SendFrame(headers={"destination": "q"}))) for _ in range(2)]
        sent = [await peer.outgoing.get(), await peer.outgoing.get()]
        frame = ErrorFrame(headers={})
        if correlation != "absent":
            frame.headers["receipt-id"] = (
                str(sent[0].headers.get("receipt")) if correlation == "matched" else "unrelated"
            )
        peer.incoming.put_nowait(frame)
        outcomes = await asyncio.gather(*commands, return_exceptions=True)
        assert isinstance(outcomes[0], ReceiptRejectedError if correlation == "matched" else ConnectionLostError)
        assert isinstance(outcomes[1], ConnectionLostError)
        cause = session.ended.result()
        assert isinstance(cause, ConnectionLostError)
        assert isinstance(cause.reason, BrokerError)
        assert cause.reason.frame is frame
        await wait_until(lambda: peer.closed)
        with pytest.raises(ConnectionLostError):
            await session.write(SendFrame(headers={"destination": "q"}), Unconfirmed())
        assert peer.outgoing.empty()
        assert session.receipts.pending_count == 0


async def test_rejection_during_drain_is_not_replaced_by_cleanup_cancellation() -> None:
    async with connection() as (peer, session):
        peer.drain.clear()
        sending = asyncio.create_task(session.write(SendFrame(headers={"destination": "q"})))
        sent = await peer.outgoing.get()
        peer.incoming.put_nowait(
            ErrorFrame(headers={"message": "denied", "receipt-id": str(sent.headers.get("receipt"))})
        )
        with pytest.raises(ReceiptRejectedError):
            await sending
        await wait_until(lambda: peer.closed)


async def test_receipt_before_terminal_error_completes_both_command_milestones() -> None:
    async with connection() as (peer, session):
        peer.drain.clear()
        command = session.command(SendFrame(headers={"destination": "q"}))
        submitting = asyncio.create_task(command.submit())
        sent = await peer.outgoing.get()
        receipt = ReceiptFrame(headers={"receipt-id": str(sent.headers.get("receipt"))})
        peer.incoming.put_nowait(receipt)
        peer.incoming.put_nowait(ErrorFrame(headers={"message": "unrelated error"}))
        await submitting
        assert await command.complete() is receipt
        await wait_until(lambda: peer.closed)


async def test_session_failure_before_command_execution_closes_without_a_supervisor() -> None:
    async with connection() as (peer, session):
        command = session.command(SendFrame(headers={"destination": "q"}))
        session.fail(ConnectionLostError(reason="connection ended before submission"))
        with pytest.raises(ConnectionLostError):
            await command.complete()
        await wait_until(lambda: peer.closed)
        assert peer.outgoing.empty()
        assert session.receipts.pending_count == 0


async def test_error_observers_cannot_start_another_command() -> None:
    observed = asyncio.Event()

    def receive(frame: AnyServerFrame, session: Session) -> None:
        assert isinstance(frame, ErrorFrame)
        assert not session.is_alive()
        with pytest.raises(ConnectionLostError):
            session.command(SendFrame(headers={"destination": "q"}))
        observed.set()

    async with connection(receive=receive) as (peer, _session):
        peer.incoming.put_nowait(ErrorFrame(headers={"message": "terminal failure"}))
        await observed.wait()
        await wait_until(lambda: peer.closed)
        assert peer.outgoing.empty()


async def test_disconnect_is_the_last_write_and_owns_its_receipt() -> None:
    async with connection(Heartbeat(10, 0)) as (peer, session):
        closing = asyncio.create_task(session.close(graceful=True))
        disconnect = await peer.outgoing.get()
        assert isinstance(disconnect, DisconnectFrame)
        with pytest.raises(ConnectionLostError):
            await session.write(SendFrame(headers={"destination": "q"}), Unconfirmed())
        await asyncio.sleep(0.025)
        assert peer.heartbeats == 0
        assert peer.outgoing.empty()
        assert not closing.done()
        peer.incoming.put_nowait(ReceiptFrame(headers={"receipt-id": "unrelated"}))
        await asyncio.sleep(0)
        assert not closing.done()
        peer.incoming.put_nowait(ReceiptFrame(headers={"receipt-id": disconnect.headers["receipt"]}))
        await closing
        assert peer.closed
