"""Bounded admission and session-bound settlement.

A reservation stays charged until both handler execution and broker settlement
finish. A channel cannot acquire another session, including during recovery.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import partial
from typing import Any

from .capacity import Capacity, Completion
from .config import Confirmation, DeliveryLimits, Paused, Unbounded
from .frames import AckMode, MessageFrame
from .session import Session
from .settlement import AutomaticAcknowledgements, ManualAcknowledgements, Settlement

LOGGER = logging.getLogger("stompman")


class UnboundedSlots:
    async def acquire(self) -> None:
        pass

    def release(self) -> None:
        pass


def handler_slots(limit: int | Unbounded | Paused) -> asyncio.Semaphore | UnboundedSlots:
    if isinstance(limit, Unbounded):
        return UnboundedSlots()
    if isinstance(limit, Paused):
        return asyncio.Semaphore(0)
    return asyncio.Semaphore(limit)


@dataclass(frozen=True, kw_only=True, slots=True)
class Delivery(MessageFrame):
    _settlement: Settlement

    async def ack(self) -> None:
        await self._settlement.settle(accepted=True)

    async def nack(self) -> None:
        await self._settlement.settle(accepted=False)


@dataclass(frozen=True, slots=True)
class Work:
    frame: MessageFrame
    channel: "Channel"
    handler: Callable[[Delivery], Awaitable[Any]]
    handled: Completion
    settlement: Settlement

    async def run(self) -> None:
        delivery = Delivery(headers=self.frame.headers, body=self.frame.body, _settlement=self.settlement)
        try:
            await self.handler(delivery)
        except Exception:
            LOGGER.exception("unhandled exception in message handler")

    def discard(self) -> None:
        self.settlement.retire()
        self.handled.finish()


class Channel:
    def __init__(
        self,
        pool: "Deliveries",
        session: Session,
        subscription_id: str,
        *,
        ack: AckMode,
        handler: Callable[[Delivery], Awaitable[Any]],
        confirmation: Confirmation,
    ) -> None:
        self._pool = pool
        self._handler = handler
        self._acknowledgements = (
            AutomaticAcknowledgements()
            if ack == "auto"
            else ManualAcknowledgements(session, subscription_id, confirmation, cumulative=ack == "client")
        )
        self._accepting = True

    def admit(self, frame: MessageFrame) -> None:
        if not self._accepting:
            return
        reservation = self._pool.capacity.reserve(frame)
        settlement = self._acknowledgements.register(frame, reservation.settled)
        message = MessageFrame(headers=frame.headers.copy(), body=frame.body)
        self._pool.enqueue(Work(message, self, self._handler, reservation.handled, settlement))

    def pause(self) -> None:
        self._accepting = False
        self._pool.drop(self)

    def close(self) -> None:
        self.pause()
        self._acknowledgements.close()

    async def finish(self) -> None:
        """Retire only after already requested settlements finish in wire order."""
        self.pause()
        await self._acknowledgements.finish()


class Deliveries:
    def __init__(self, limits: DeliveryLimits, group: asyncio.TaskGroup) -> None:
        self.capacity = Capacity(limits)
        self._group = group
        self._slots = handler_slots(limits.concurrency)
        self._queue: deque[Work] = deque()
        self._available = asyncio.Event()
        self._handlers: set[asyncio.Task[None]] = set()

    @property
    def pending_messages(self) -> int:
        return self.capacity.pending_messages

    @property
    def pending_bytes(self) -> int:
        return self.capacity.pending_bytes

    @property
    def running_handlers(self) -> int:
        return len(self._handlers)

    def is_handler(self) -> bool:
        return asyncio.current_task() in self._handlers

    def enqueue(self, work: Work) -> None:
        self._queue.append(work)
        self._available.set()

    def drop(self, channel: Channel) -> None:
        remaining: deque[Work] = deque()
        for work in self._queue:
            if work.channel is channel:
                work.discard()
            else:
                remaining.append(work)
        self._queue = remaining
        if not self._queue:
            self._available.clear()

    async def dispatch(self) -> None:
        while True:
            await self._available.wait()
            await self._slots.acquire()
            if not self._queue:
                self._slots.release()
                continue
            work = self._queue.popleft()
            if not self._queue:
                self._available.clear()
            task = self._group.create_task(work.run(), name="stomp-handler")
            self._handlers.add(task)
            task.add_done_callback(partial(self._finished, work=work))

    def _finished(self, task: asyncio.Task[None], work: Work) -> None:
        work.handled.finish()
        self._handlers.discard(task)
        self._slots.release()

    async def drain(self, *, cancel: bool) -> None:
        if cancel:
            for task in self._handlers:
                task.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)
