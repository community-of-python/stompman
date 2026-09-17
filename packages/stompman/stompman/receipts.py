import asyncio
from dataclasses import dataclass, field
from typing import Literal, Protocol
from uuid import uuid4

from stompman.connection import AbstractConnection
from stompman.errors import Error
from stompman.frames import ErrorFrame, ReceiptFrame

ReceiptFailureReason = Literal["rejected", "timeout", "connection_lost"]


class ReceiptWaiter(Protocol):
    def confirm(self, pending: "PendingReceipt") -> None: ...
    def fail_on_error_frame(self, pending: "PendingReceipt", frame: ErrorFrame) -> None: ...
    def fail_on_connection_loss(self, pending: "PendingReceipt") -> None: ...


@dataclass(kw_only=True, slots=True, frozen=True)
class PendingReceipt:
    waiter: ReceiptWaiter
    connection: AbstractConnection
    receipt_id: str
    epoch: int
    deadline: float
    result: asyncio.Future[Error | None]


@dataclass(kw_only=True, slots=True, frozen=True)
class PendingReceipts:
    """Receipts awaited on the current connection, keyed by receipt ID."""

    pending: dict[str, PendingReceipt] = field(default_factory=dict, init=False)
    started: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def add(self, pending: PendingReceipt) -> None:
        self.pending[pending.receipt_id] = pending
        self.started.set()

    def discard(self, receipt_id: str) -> bool:
        if self.pending.pop(receipt_id, None) is None:
            return False
        if not self.pending:
            self.started.clear()
        return True

    def handle_receipt(self, frame: ReceiptFrame, *, epoch: int) -> None:
        if (pending := self.pending.get(frame.headers["receipt-id"])) and pending.epoch == epoch:
            pending.waiter.confirm(pending)

    def handle_error(self, frame: ErrorFrame, *, epoch: int) -> None:
        receipt_id = frame.headers.get("receipt-id")
        if receipt_id is not None:
            pending = self.pending.get(receipt_id)
            affected = [pending] if pending is not None else []
        else:
            # An uncorrelated ERROR cannot safely confirm anything in flight.
            affected = list(self.pending.values())
        for pending in affected:
            if pending.epoch == epoch:
                pending.waiter.fail_on_error_frame(pending, frame)

    def connection_lost(self, connection: AbstractConnection) -> None:
        for pending in list(self.pending.values()):
            if pending.connection is connection:
                pending.waiter.fail_on_connection_loss(pending)


async def wait_for_result(pending: PendingReceipt) -> Error | None:
    """Wait for the receipt, raising TimeoutError only if the deadline passed with nothing delivered."""
    try:
        if pending.result.done():
            return pending.result.result()
        async with asyncio.timeout_at(pending.deadline):
            return await asyncio.shield(pending.result)
    except TimeoutError:
        # A receipt already processed by the reader beats a deadline cancellation delivered before we were rescheduled.
        if pending.result.done():
            return pending.result.result()
        raise


def make_receipt_id() -> str:
    return str(uuid4())
