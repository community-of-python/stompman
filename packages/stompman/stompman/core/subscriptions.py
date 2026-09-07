"""Subscription intent, installation, and restoration have a single owner."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from types import MappingProxyType
from typing import Any

from ._tasks import await_cleanup
from .config import Confirmation, Confirmed, Unconfirmed
from .delivery import Channel, Deliveries, Delivery
from .errors import ConnectionLostError, ReceiptRejectedError, ReceiptTimeoutError, SubscriptionError
from .frames import AckMode, MessageFrame, ReceiptFrame, SubscribeFrame, UnsubscribeFrame
from .recovery import ConnectionSupervisor
from .session import Command, Session

LOGGER = logging.getLogger("stompman")


def log_subscription_error(error: SubscriptionError) -> None:
    LOGGER.warning("subscription confirmation failed: %s", error)


@dataclass(frozen=True, slots=True)
class SubscriptionSpec:
    id: str
    destination: str
    ack: AckMode
    headers: Mapping[str, str]
    handler: Callable[[Delivery], Awaitable[Any]]
    confirmation: Confirmation
    operations: Confirmation
    on_error: Callable[[SubscriptionError], Any]

    def __post_init__(self) -> None:
        if not self.id or not self.destination:
            msg = "subscription id and destination must not be empty"
            raise ValueError(msg)
        if self.ack not in {"auto", "client", "client-individual"}:
            msg = "unsupported acknowledgement mode"
            raise ValueError(msg)
        object.__setattr__(self, "headers", MappingProxyType(dict(self.headers)))

    def frame(self) -> SubscribeFrame:
        return SubscribeFrame.build(
            subscription_id=self.id, destination=self.destination, ack=self.ack, headers=dict(self.headers)
        )


class Dormant(Enum):
    NEW = auto()
    REMOVED = auto()


@dataclass(frozen=True, slots=True)
class Active:
    session: Session
    channel: Channel


@dataclass(slots=True)
class Installing:
    session: Session
    channel: Channel
    command: Command
    operation: asyncio.Task[ReceiptFrame | None]


@dataclass(frozen=True, slots=True)
class Rejected:
    error: SubscriptionError


class Subscription:
    def __init__(self, spec: SubscriptionSpec, owner: "Subscriptions") -> None:
        self.spec = spec
        self._owner = owner
        self._state: Dormant | Active | Installing | Rejected = Dormant.NEW

    @property
    def id(self) -> str:
        return self.spec.id

    @property
    def key(self) -> tuple[str, str]:
        return "subscription", self.id

    @property
    def destination(self) -> str:
        return self.spec.destination

    @property
    def ack(self) -> AckMode:
        return self.spec.ack

    @property
    def headers(self) -> Mapping[str, str]:
        return self.spec.headers

    def receive(self, frame: MessageFrame, session: Session) -> None:
        state = self._state
        if isinstance(state, (Active, Installing)) and state.session is session:
            state.channel.admit(frame)

    def pause(self) -> None:
        if isinstance(self._state, (Active, Installing)):
            self._state.channel.pause()

    def disconnected(self, session: Session) -> None:
        if isinstance(self._state, (Active, Installing)) and self._state.session is session:
            self._state.channel.close()

    def _remove(self, state: Dormant | Rejected) -> None:
        if isinstance(self._state, (Active, Installing)):
            self._state.channel.close()
        self._state = state
        self._owner.remove(self)

    def retire(self) -> None:
        self._remove(Dormant.REMOVED)

    def _notify(self, error: SubscriptionError) -> None:
        try:
            self.spec.on_error(error)
        except Exception:
            LOGGER.exception("unhandled exception in subscription error callback")

    def _rejected(self, cause: ReceiptRejectedError) -> None:
        failure = SubscriptionError(subscription_id=self.id, reason="rejected", frame=cause.frame)
        self._remove(Rejected(failure))
        self._notify(failure)

    async def _submit(self, session: Session) -> Installing:
        command = session.command(self.spec.frame(), self.spec.confirmation, self._rejected)
        channel = Channel(
            self._owner.deliveries,
            session,
            self.id,
            ack=self.spec.ack,
            handler=self.spec.handler,
            confirmation=self.spec.operations,
        )

        async def submit() -> None:
            await command.submit()

        attempt = Installing(session, channel, command, asyncio.create_task(submit(), name="stomp-subscribe-write"))
        self._state = attempt
        try:
            await attempt.operation
        except BaseException:
            command.close()
            raise
        return attempt

    async def _complete(self, attempt: Installing) -> None:
        attempt.operation = asyncio.create_task(attempt.command.complete(), name="stomp-subscribe-receipt")
        try:
            await attempt.operation
        finally:
            attempt.command.close()
        if self._state is attempt:
            self._state = Active(attempt.session, attempt.channel)

    async def _cleanup(self, session: Session) -> None:
        # A rejection callback may already have installed a replacement with this ID.
        # Otherwise this write queues before a later replacement can submit SUBSCRIBE.
        if self.id in self._owner.ids:
            return
        confirmation = self.spec.confirmation
        if isinstance(confirmation, Confirmed) and not session.ended.done():
            with suppress(ConnectionLostError, TimeoutError):
                async with asyncio.timeout(confirmation.timeout):
                    await session.write(UnsubscribeFrame(headers={"id": self.id}), Unconfirmed())

    async def _failed(self, cause: BaseException, session: Session, *, restoring: bool, cancelled: bool) -> None:
        if isinstance(self._state, Rejected):
            failure = self._state.error
        else:
            reason = (
                "rejected"
                if isinstance(cause, ReceiptRejectedError)
                else "timeout"
                if isinstance(cause, (TimeoutError, ReceiptTimeoutError))
                else "unsubscribed"
                if isinstance(cause, asyncio.CancelledError) and not restoring
                else "connection_lost"
            )
            failure = SubscriptionError(subscription_id=self.id, reason=reason)  # type: ignore[arg-type]
            self._remove(Rejected(failure))
            if (not cancelled and failure.reason != "unsubscribed") or restoring:
                self._notify(failure)
        await await_cleanup(asyncio.create_task(self._cleanup(session)))
        if not cancelled:
            raise failure from cause

    async def open(self) -> None:
        caller = asyncio.current_task()
        assert caller is not None  # ruff: ignore[assert]
        cancelling = caller.cancelling()
        confirmation = self.spec.confirmation

        async def submit(session: Session) -> Installing:
            self._owner.add(self)
            try:
                return await self._submit(session)
            except BaseException as cause:
                if isinstance(confirmation, Confirmed):
                    await self._failed(cause, session, restoring=False, cancelled=caller.cancelling() > cancelling)
                else:
                    self.retire()
                raise

        attempts = confirmation.attempts if isinstance(confirmation, Unconfirmed) else 1
        attempt = await self._owner.connections.run(submit, attempts=attempts)
        try:
            await self._complete(attempt)
        except BaseException as cause:
            await self._failed(cause, attempt.session, restoring=False, cancelled=caller.cancelling() > cancelling)
            raise

    async def restore(self, session: Session) -> None:
        if not isinstance(self._state, Active):
            return
        caller = asyncio.current_task()
        assert caller is not None  # ruff: ignore[assert]
        cancelling = caller.cancelling()
        try:
            attempt = await self._submit(session)
            await self._complete(attempt)
        except BaseException as cause:
            if isinstance(self.spec.confirmation, Unconfirmed):
                raise
            with suppress(SubscriptionError):
                await self._failed(cause, session, restoring=True, cancelled=caller.cancelling() > cancelling)
            if isinstance(cause, asyncio.CancelledError) and caller.cancelling() > cancelling:
                raise

    async def unsubscribe(self) -> None:
        state = self._state
        if isinstance(state, Installing):
            self._remove(Rejected(SubscriptionError(subscription_id=self.id, reason="unsubscribed")))
            state.operation.cancel()
        elif isinstance(state, Active):
            self._state = Dormant.REMOVED
            self._owner.remove(self)
            state.channel.pause()
            await await_cleanup(asyncio.create_task(self._unsubscribe_active(state)))

    async def _unsubscribe_active(self, state: Active) -> None:
        await state.channel.finish()
        if not state.session.ended.done() and self.id not in self._owner.ids:
            with suppress(ConnectionLostError):
                await state.session.write(UnsubscribeFrame(headers={"id": self.id}), self.spec.operations)


class Subscriptions:
    def __init__(self, connections: ConnectionSupervisor, deliveries: Deliveries) -> None:
        self.connections = connections
        self.deliveries = deliveries
        self._items: dict[str, Subscription] = {}
        self._empty = asyncio.Event()
        self._empty.set()

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(self._items)

    def add(self, subscription: Subscription) -> None:
        self.connections.attach(subscription)
        self._items[subscription.id] = subscription
        self._empty.clear()

    def remove(self, subscription: Subscription) -> None:
        self.connections.detach(subscription)
        if self._items.get(subscription.id) is subscription:
            self._items.pop(subscription.id)
        if not self._items:
            self._empty.set()

    async def subscribe(self, spec: SubscriptionSpec) -> Subscription:
        subscription = Subscription(spec, self)
        await subscription.open()
        return subscription

    def receive(self, frame: MessageFrame, session: Session) -> None:
        subscription = self._items.get(frame.headers["subscription"])
        if subscription is not None:
            subscription.receive(frame, session)

    def pause(self) -> None:
        for subscription in self._items.values():
            subscription.pause()

    async def close(self) -> None:
        for subscription in tuple(self._items.values()):
            await subscription.unsubscribe()

    async def wait_empty(self) -> None:
        await self._empty.wait()
