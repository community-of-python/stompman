"""Receipt correlation has one owner and is retired before rejection observers run."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from uuid import uuid4

from .errors import ConnectionLostError, ReceiptRejectedError
from .frames import ErrorFrame, ReceiptFrame


def ignore_rejection(error: ReceiptRejectedError) -> None:
    del error


@dataclass(frozen=True, slots=True)
class Receipt:
    id: str
    result: asyncio.Future[ReceiptFrame]
    rejected: Callable[[ReceiptRejectedError], None]


class Receipts:
    def __init__(self) -> None:
        self._pending: dict[str, Receipt] = {}

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def reserve(self, rejected: Callable[[ReceiptRejectedError], None]) -> Receipt:
        receipt = Receipt(str(uuid4()), asyncio.get_running_loop().create_future(), rejected)
        self._pending[receipt.id] = receipt
        return receipt

    def discard(self, receipt: Receipt) -> None:
        self._pending.pop(receipt.id, None)
        if receipt.result.done() and not receipt.result.cancelled():
            receipt.result.exception()
        else:
            receipt.result.cancel()

    def receive(self, frame: ReceiptFrame | ErrorFrame) -> None:
        if isinstance(frame, ReceiptFrame):
            receipt = self._pending.pop(frame.headers["receipt-id"], None)
            if receipt is not None and not receipt.result.done():
                receipt.result.set_result(frame)
            return
        receipt_id = frame.headers.get("receipt-id")
        rejected = [item for item in self._pending.values() if receipt_id is None or item.id == receipt_id]
        # Retire all correlations before callbacks may inspect the subscription registry.
        for receipt in rejected:
            self._pending.pop(receipt.id)
        for receipt in rejected:
            if not receipt.result.done():
                error = ReceiptRejectedError(receipt_id=receipt.id, frame=frame)
                receipt.result.set_exception(error)
                receipt.rejected(error)

    def fail(self, error: Exception) -> None:
        pending, self._pending = self._pending, {}
        for receipt in pending.values():
            if not receipt.result.done():
                receipt.result.set_exception(ConnectionLostError(reason=error))
