"""Subscription intent, installation, and restoration have a single owner."""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from enum import Enum, auto
from types import MappingProxyType
from typing import Any

from ._tasks import Cancellation, await_cleanup
from .command import Command
from .config import Confirmation, Confirmed, Unconfirmed
from .delivery import Channel, Deliveries, Delivery
from .errors import ConnectionLostError, ReceiptRejectedError, ReceiptTimeoutError, SubscriptionError
from .frames import AckMode, MessageFrame, SubscribeFrame, UnsubscribeFrame
from .recovery import ConnectionSupervisor
from .session import Session

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
    WAITING_FOR_SESSION = auto()
    REMOVED = auto()


class Installation(Enum):
    INITIAL = auto()
    RESTORATION = auto()


@dataclass(frozen=True, slots=True)
class Active:
    session: Session
    channel: Channel


@dataclass(frozen=True, slots=True)
class Installing:
    session: Session
    channel: Channel
    command: Command


@dataclass(frozen=True, slots=True)
class Rejected:
    error: SubscriptionError


@dataclass(frozen=True, slots=True)
class Removing:
    task: asyncio.Task[None]


class Subscription:
    def __init__(self, source: Callable[[], SubscriptionSpec], owner: "Subscriptions") -> None:
        self._source = source
        self._spec = source()
        self._owner = owner
        self._state: Dormant | Active | Installing | Rejected | Removing = Dormant.NEW

    @property
    def spec(self) -> SubscriptionSpec:
        return self._spec

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
            session.validate_delivery(frame, self.ack)
            state.channel.admit(frame)

    def pause(self) -> None:
        if isinstance(self._state, (Active, Installing)):
            self._state.channel.pause()

    def disconnected(self, session: Session) -> None:
        state = self._state
        if isinstance(state, (Active, Installing)) and state.session is session:
            state.channel.close()
            if isinstance(state, Active):
                self._state = Dormant.WAITING_FOR_SESSION

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

        attempt = Installing(session, channel, command)
        self._state = attempt
        await command.submit()
        return attempt

    async def _complete(self, attempt: Installing) -> None:
        await attempt.command.complete()
        if isinstance(self._state, Rejected):
            raise self._state.error
        if self._state is attempt:
            attempt.session.check()
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

    def _failure(self, cause: BaseException, installation: Installation) -> SubscriptionError:
        if isinstance(cause, ReceiptRejectedError):
            return SubscriptionError(subscription_id=self.id, reason="rejected", frame=cause.frame)
        if isinstance(cause, (TimeoutError, ReceiptTimeoutError)):
            return SubscriptionError(subscription_id=self.id, reason="timeout")
        if isinstance(cause, asyncio.CancelledError) and installation is Installation.INITIAL:
            return SubscriptionError(subscription_id=self.id, reason="unsubscribed")
        return SubscriptionError(subscription_id=self.id, reason="connection_lost")

    async def _failed(
        self, cause: BaseException, session: Session, installation: Installation, cancellation: Cancellation
    ) -> SubscriptionError:
        if isinstance(self._state, Rejected):
            failure = self._state.error
        else:
            failure = self._failure(cause, installation)
            self._remove(Rejected(failure))
            if installation is Installation.RESTORATION or (
                not cancellation.requested and failure.reason != "unsubscribed"
            ):
                self._notify(failure)
        await await_cleanup(asyncio.create_task(self._cleanup(session)))
        return failure

    async def open(self) -> None:
        cancellation = Cancellation.capture()
        confirmation = self.spec.confirmation

        async def submit(session: Session) -> Installing:
            self._owner.add(self)
            try:
                return await self._submit(session)
            except BaseException as cause:
                if isinstance(confirmation, Confirmed):
                    failure = await self._failed(cause, session, Installation.INITIAL, cancellation)
                    if not cancellation.requested:
                        raise failure from cause
                else:
                    self.retire()
                raise

        attempts = confirmation.attempts if isinstance(confirmation, Unconfirmed) else 1
        attempt = await self._owner.connections.run(submit, attempts=attempts)
        try:
            await self._complete(attempt)
        except BaseException as cause:
            failure = await self._failed(cause, attempt.session, Installation.INITIAL, cancellation)
            if not cancellation.requested:
                raise failure from cause
            raise

    async def restore(self, session: Session) -> None:
        if self._state is not Dormant.WAITING_FOR_SESSION:
            return
        previous = self._spec
        self._spec = self._source()
        try:
            self._owner.rekey(self, previous.id)
        except BaseException:
            self._spec = previous
            raise
        cancellation = Cancellation.capture()
        try:
            attempt = await self._submit(session)
            await self._complete(attempt)
        except BaseException as cause:
            if isinstance(self.spec.confirmation, Unconfirmed):
                if isinstance(self._state, Installing) and self._state.session is session:
                    self._state.channel.close()
                    self._state = Dormant.WAITING_FOR_SESSION
                raise
            await self._failed(cause, session, Installation.RESTORATION, cancellation)
            if cancellation.requested:
                raise

    async def unsubscribe(self) -> None:
        state = self._state
        if state is Dormant.WAITING_FOR_SESSION:
            self.retire()
        elif isinstance(state, Installing):
            self._remove(Rejected(SubscriptionError(subscription_id=self.id, reason="unsubscribed")))
            state.command.cancel()
        elif isinstance(state, Active):
            state.channel.pause()
            state = Removing(asyncio.create_task(self._unsubscribe_active(state), name="stomp-unsubscribe"))
            self._state = state
        if isinstance(state, Removing):
            try:
                await await_cleanup(state.task)
            finally:
                self.retire()

    async def _unsubscribe_active(self, state: Active) -> None:
        try:
            await state.channel.finish()
            if not state.session.ended.done():
                await state.session.write(UnsubscribeFrame(headers={"id": self.id}), self.spec.operations)
        except ConnectionLostError:
            pass
        except BaseException:
            # The ID remains reserved until removal is confirmed or this session
            # can no longer accept a replacement with the same ID.
            state.session.fail(ConnectionLostError(reason="UNSUBSCRIBE was not confirmed"))
            raise


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

    def rekey(self, subscription: Subscription, previous_id: str) -> None:
        existing = self._items.get(subscription.id)
        if existing is not None and existing is not subscription:
            msg = "subscription id is already active"
            raise ValueError(msg)
        self.connections.rekey(subscription, ("subscription", previous_id))
        if self._items.get(previous_id) is subscription:
            self._items.pop(previous_id)
        self._items[subscription.id] = subscription

    async def subscribe(self, spec: SubscriptionSpec) -> Subscription:
        return await self.follow(lambda: spec)

    async def follow(self, source: Callable[[], SubscriptionSpec]) -> Subscription:
        subscription = Subscription(source, self)
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
