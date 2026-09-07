"""An owned command has two observable milestones and one cleanup lifetime.

The command starts when created. Submission waits for transport drain; completion
also waits for the receipt. Cancelling either waiter retires the entire command.
Callers never reserve receipts, close commands, or coordinate execution phases.
"""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING

from ._tasks import await_cleanup
from .config import Confirmation, Confirmed
from .errors import ConnectionLostError, ReceiptRejectedError, ReceiptTimeoutError
from .frames import AnyClientFrame, ReceiptFrame
from .receipts import Receipts

if TYPE_CHECKING:
    from .session import Session


class Command:
    def __init__(
        self,
        owner: "Commands",
        session: "Session",
        frame: AnyClientFrame,
        confirmation: Confirmation,
        rejected: Callable[[ReceiptRejectedError], None],
    ) -> None:
        self._owner = owner
        self._session = session
        self._submitted: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._execute(frame, confirmation, rejected), name="stomp-command")
        self._task.add_done_callback(self._finished)

    async def _transmit(self, frame: AnyClientFrame) -> None:
        await self._session.transmit(frame)
        self._submitted.set_result(None)

    async def _execute(
        self, frame: AnyClientFrame, confirmation: Confirmation, rejected: Callable[[ReceiptRejectedError], None]
    ) -> ReceiptFrame | None:
        if not isinstance(confirmation, Confirmed):
            await self._transmit(frame)
            return None

        receipt = self._owner.receipts.reserve(rejected)
        outgoing = replace(frame, headers=frame.headers | {"receipt": receipt.id})  # type: ignore[arg-type]
        try:
            async with asyncio.timeout(confirmation.timeout):
                await self._transmit(outgoing)
                return await receipt.result
        except TimeoutError as error:
            raise ReceiptTimeoutError(receipt_id=receipt.id, timeout=confirmation.timeout) from error
        finally:
            self._owner.receipts.discard(receipt)

    def _finished(self, task: asyncio.Task[ReceiptFrame | None]) -> None:
        self._owner.retire(self)
        if task.cancelled():
            if not self._submitted.done():
                self._submitted.cancel()
            return
        error = task.exception()
        if not self._submitted.done() and error is not None:
            self._submitted.set_exception(error)
            # Completion may be the only waiter; both milestones carry the error.
            self._submitted.exception()

    async def submit(self) -> None:
        """Wait for drain, leaving receipt completion independent of the caller."""
        try:
            await asyncio.shield(self._submitted)
        except BaseException:
            self.cancel()
            with suppress(BaseException):
                await await_cleanup(self._task)
            raise

    async def complete(self) -> ReceiptFrame | None:
        return await self._task

    def cancel(self) -> None:
        self._task.cancel()

    def invalidate(self, reason: str) -> None:
        self._session.fail(ConnectionLostError(reason=reason))


class Commands:
    """Own every command and receipt until completion or session shutdown."""

    def __init__(self) -> None:
        self.receipts = Receipts()
        self._active: set[Command] = set()

    def start(
        self,
        session: "Session",
        frame: AnyClientFrame,
        confirmation: Confirmation,
        rejected: Callable[[ReceiptRejectedError], None],
    ) -> Command:
        command = Command(self, session, frame, confirmation, rejected)
        self._active.add(command)
        return command

    def retire(self, command: Command) -> None:
        self._active.discard(command)

    async def close(self) -> None:
        commands = tuple(self._active)
        for command in commands:
            command.cancel()
        await asyncio.gather(*(command.complete() for command in commands), return_exceptions=True)
