"""Bounded admission and session-bound settlement.

A reservation stays charged until both handler execution and broker settlement
finish. A channel cannot acquire another session, including during recovery.
"""

import asyncio
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import Enum, auto
from functools import partial
from typing import Any

from .config import Confirmation, DeliveryLimits
from .errors import ConnectionLostError, ConsumerOverloadedError
from .frames import AckFrame, AckMode, MessageFrame, NackFrame
from .session import Session

LOGGER = logging.getLogger("stompman")


class Obligation(Enum):
    HANDLER = auto()
    SETTLEMENT = auto()


class Decision(Enum):
    WAITING = auto()
    ACCEPT = auto()
    REJECT = auto()
    RETIRED = auto()


class Reservation:
    def __init__(self, pool: "Deliveries", size: int, obligations: set[Obligation]) -> None:
        self._pool = pool
        self.size = size
        self._obligations = obligations

    def finish(self, obligation: Obligation) -> None:
        self._obligations.discard(obligation)
        if not self._obligations:
            self._pool.release(self)


class Settlement:
    def __init__(self, channel: "Channel", ack_id: str, reservation: Reservation, decision: Decision) -> None:
        self.channel = channel
        self.ack_id = ack_id
        self.reservation = reservation
        self.decision = decision

    async def settle(self, *, accepted: bool) -> None:
        await self.channel.settle(self, accepted=accepted)

    def retire(self) -> None:
        self.decision = Decision.RETIRED
        self.reservation.finish(Obligation.SETTLEMENT)


@dataclass(frozen=True, kw_only=True, slots=True)
class Delivery(MessageFrame):
    _settlement: Settlement

    async def ack(self) -> None:
        await self._settlement.settle(accepted=True)

    async def nack(self) -> None:
        await self._settlement.settle(accepted=False)


@dataclass(frozen=True, slots=True)
class Work:
    delivery: Delivery
    channel: "Channel"
    reservation: Reservation


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
        self._session = session
        self._id = subscription_id
        self._ack = ack
        self.handler = handler
        self._confirmation = confirmation
        self._unsettled: deque[Settlement] = deque()
        self._settling = asyncio.Lock()
        self._accepting = True

    def admit(self, frame: MessageFrame) -> None:
        if not self._accepting:
            return
        obligations = {Obligation.HANDLER} if self._ack == "auto" else set(Obligation)
        reservation = self._pool.reserve(frame, obligations)
        settlement = Settlement(
            self,
            frame.headers.get("ack", ""),
            reservation,
            Decision.RETIRED if self._ack == "auto" else Decision.WAITING,
        )
        if self._ack != "auto":
            self._unsettled.append(settlement)
        self._pool.enqueue(
            Work(Delivery(headers=frame.headers.copy(), body=frame.body, _settlement=settlement), self, reservation)
        )

    def pause(self) -> None:
        self._accepting = False
        self._pool.drop(self)

    def close(self) -> None:
        self.pause()
        for settlement in self._unsettled:
            settlement.retire()
        self._unsettled.clear()

    async def finish(self) -> None:
        """Retire only after already requested settlements finish in wire order."""
        async with self._settling:
            self.close()

    async def settle(self, settlement: Settlement, *, accepted: bool) -> None:
        async with self._settling:
            if settlement.decision is not Decision.WAITING:
                return
            settlement.decision = Decision.ACCEPT if accepted else Decision.REJECT
            if self._ack == "client":
                while self._unsettled and self._unsettled[0].decision is not Decision.WAITING:
                    await self._send_settlement(self._unsettled.popleft())
            else:
                self._unsettled.remove(settlement)
                await self._send_settlement(settlement)

    async def _send_settlement(self, settlement: Settlement) -> None:
        try:
            if settlement.decision is Decision.RETIRED or self._session.ended.done():
                return
            if not settlement.ack_id:
                LOGGER.warning("failed to settle message frame: it has no ack header")
                return
            frame_type = AckFrame if settlement.decision is Decision.ACCEPT else NackFrame
            await self._session.write(
                frame_type(headers={"id": settlement.ack_id, "subscription": self._id}), self._confirmation
            )
        except ConnectionLostError:
            pass
        finally:
            settlement.retire()


class Deliveries:
    def __init__(self, limits: DeliveryLimits, group: asyncio.TaskGroup) -> None:
        self._limits = limits
        self._group = group
        self._slots = asyncio.Semaphore(limits.concurrency)
        self._queue: deque[Work] = deque()
        self._available = asyncio.Event()
        self._reservations: set[Reservation] = set()
        self._bytes = 0
        self._handlers: set[asyncio.Task[None]] = set()

    @property
    def pending_messages(self) -> int:
        return len(self._reservations)

    @property
    def pending_bytes(self) -> int:
        return self._bytes

    @property
    def running_handlers(self) -> int:
        return len(self._handlers)

    def is_handler(self) -> bool:
        return asyncio.current_task() in self._handlers

    def reserve(self, frame: MessageFrame, obligations: set[Obligation]) -> Reservation:
        size = len(frame.body) + sum(
            len(key.encode()) + len(str(value).encode()) for key, value in frame.headers.items()
        )
        if len(self._reservations) >= self._limits.pending_messages or self._bytes + size > self._limits.pending_bytes:
            raise ConsumerOverloadedError(
                max_pending_messages=self._limits.pending_messages, max_pending_bytes=self._limits.pending_bytes
            )
        reservation = Reservation(self, size, obligations)
        self._reservations.add(reservation)
        self._bytes += size
        return reservation

    def release(self, reservation: Reservation) -> None:
        if reservation in self._reservations:
            self._reservations.remove(reservation)
            self._bytes -= reservation.size

    def enqueue(self, work: Work) -> None:
        self._queue.append(work)
        self._available.set()

    def drop(self, channel: Channel) -> None:
        remaining: deque[Work] = deque()
        for work in self._queue:
            if work.channel is channel:
                work.delivery._settlement.retire()
                work.reservation.finish(Obligation.HANDLER)
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
            task = self._group.create_task(self._run(work), name="stomp-handler")
            self._handlers.add(task)
            task.add_done_callback(partial(self._finished, work=work))

    def _finished(self, task: asyncio.Task[None], work: Work) -> None:
        work.reservation.finish(Obligation.HANDLER)
        self._handlers.discard(task)
        self._slots.release()

    @staticmethod
    async def _run(work: Work) -> None:
        try:
            await work.channel.handler(work.delivery)
        except Exception:
            LOGGER.exception("unhandled exception in message handler")

    async def drain(self, *, cancel: bool) -> None:
        if cancel:
            for task in self._handlers:
                task.cancel()
        await asyncio.gather(*self._handlers, return_exceptions=True)
