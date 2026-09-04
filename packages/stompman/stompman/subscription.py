import asyncio
import math
from collections.abc import Awaitable, Callable, Coroutine
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from stompman.connection import AbstractConnection
from stompman.connection_manager import ConnectionManager
from stompman.errors import ConnectionLostError, SubscriptionError
from stompman.frames import (
    AckFrame,
    AckMode,
    ErrorFrame,
    MessageFrame,
    NackFrame,
    ReceiptFrame,
    SubscribeFrame,
    UnsubscribeFrame,
)
from stompman.logger import LOGGER


@dataclass(kw_only=True, slots=True, frozen=True)
class _PendingConfirmation:
    subscription: "BaseSubscription"
    connection: AbstractConnection
    receipt_id: str
    epoch: int
    deadline: float
    result: asyncio.Future[SubscriptionError | None]


@dataclass(kw_only=True, slots=True, frozen=True)
class ActiveSubscriptions:
    subscriptions: dict[str, "AutoAckSubscription | ManualAckSubscription"] = field(default_factory=dict, init=False)
    event: asyncio.Event = field(default_factory=asyncio.Event, init=False)
    pending_receipts: dict[str, _PendingConfirmation] = field(default_factory=dict, init=False)
    confirmation_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    confirmation_started: asyncio.Event = field(default_factory=asyncio.Event, init=False)

    def __post_init__(self) -> None:
        self.event.set()

    def get_by_id(self, subscription_id: str) -> "AutoAckSubscription | ManualAckSubscription | None":
        return self.subscriptions.get(subscription_id)

    def get_all(self) -> list["AutoAckSubscription | ManualAckSubscription"]:
        return list(self.subscriptions.values())

    def get_ids(self) -> list[str]:
        return list(self.subscriptions.keys())

    def delete_by_id(self, subscription_id: str) -> None:
        if subscription_id in self.subscriptions:
            del self.subscriptions[subscription_id]
        if not self.subscriptions:
            self.event.set()

    def add(self, subscription: "AutoAckSubscription | ManualAckSubscription") -> None:
        self.subscriptions[subscription.id] = subscription
        self.event.clear()

    def contains_by_id(self, subscription_id: str) -> bool:
        return subscription_id in self.subscriptions

    async def wait_until_empty(self) -> bool:
        return await self.event.wait()

    def handle_receipt(self, frame: ReceiptFrame, *, epoch: int) -> None:
        if (pending := self.pending_receipts.get(frame.headers["receipt-id"])) and pending.epoch == epoch:
            del self.pending_receipts[pending.receipt_id]
            if not self.pending_receipts:
                self.confirmation_started.clear()
            pending.subscription._pending_confirmation = None
            if not pending.result.done():
                pending.result.set_result(None)

    def handle_error(self, frame: ErrorFrame, *, epoch: int) -> None:
        receipt_id = frame.headers.get("receipt-id")
        if receipt_id is not None:
            pending = self.pending_receipts.get(receipt_id)
            affected = [pending] if pending is not None else []
        else:
            # An uncorrelated ERROR cannot safely confirm any in-flight
            # subscription. Confirmed subscriptions remain eligible for recovery.
            affected = list(self.pending_receipts.values())
        for pending in affected:
            if pending.epoch != epoch:
                continue
            pending.subscription._fail_confirmation(
                pending, SubscriptionError(subscription_id=pending.subscription.id, reason="rejected", frame=frame)
            )

    def connection_lost(self, connection: AbstractConnection) -> None:
        for pending in list(self.pending_receipts.values()):
            if pending.connection is connection:
                pending.subscription._fail_confirmation(
                    pending, SubscriptionError(subscription_id=pending.subscription.id, reason="connection_lost")
                )

    def watch_confirmation(self, pending: _PendingConfirmation) -> None:
        task = asyncio.create_task(pending.subscription._watch_confirmation(pending))
        self.confirmation_tasks.add(task)
        task.add_done_callback(self.confirmation_tasks.discard)

    async def cancel_confirmation_tasks(self) -> None:
        tasks = tuple(self.confirmation_tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.confirmation_tasks.difference_update(tasks)


@dataclass(kw_only=True, slots=True)
class BaseSubscription:
    id: str = field(default_factory=lambda: _make_subscription_id(), init=False)  # ruff: ignore[unnecessary-lambda]
    destination: str
    headers: dict[str, str] | None
    ack: AckMode
    _connection_manager: ConnectionManager
    _active_subscriptions: ActiveSubscriptions
    receipt_timeout: float | None = None
    on_subscription_error: Callable[[SubscriptionError], Any] | None = None
    _pending_confirmation: _PendingConfirmation | None = field(default=None, init=False, repr=False)

    async def _subscribe(self) -> None:
        if self.receipt_timeout is not None:
            if not math.isfinite(self.receipt_timeout) or self.receipt_timeout <= 0:
                msg = "receipt_timeout must be a finite positive number"
                raise ValueError(msg)
            connection_state = await self._connection_manager._get_restored_connection_state()
            self._active_subscriptions.add(self)  # type: ignore[arg-type]
            try:
                pending = await self._send_confirmed_subscription(connection_state.connection)
                await self._await_confirmation(pending)
            except ConnectionLostError as error:
                await self._connection_manager._discard_failed_connection_state(connection_state, error)
                raise SubscriptionError(subscription_id=self.id, reason="connection_lost") from error
            return
        await self._connection_manager.write_frame_reconnecting(
            SubscribeFrame.build(
                subscription_id=self.id, destination=self.destination, ack=self.ack, headers=self.headers
            )
        )
        self._active_subscriptions.add(self)  # type: ignore[arg-type]

    async def unsubscribe(self) -> None:
        if pending := self._pending_confirmation:
            self._fail_confirmation(
                pending, SubscriptionError(subscription_id=self.id, reason="unsubscribed"), notify=False
            )
        self._active_subscriptions.delete_by_id(self.id)
        await self._connection_manager.maybe_write_frame(UnsubscribeFrame(headers={"id": self.id}))

    async def _send_confirmed_subscription(
        self, connection: AbstractConnection, *, restoring: bool = False
    ) -> _PendingConfirmation:
        assert self.receipt_timeout is not None  # ruff: ignore[assert] - internal invariant
        pending = _PendingConfirmation(
            subscription=self,
            connection=connection,
            receipt_id=_make_confirmation_id(),
            epoch=self._connection_manager._reconnection_count,
            deadline=asyncio.get_running_loop().time() + self.receipt_timeout,
            result=asyncio.get_running_loop().create_future(),
        )
        self._pending_confirmation = pending
        self._active_subscriptions.pending_receipts[pending.receipt_id] = pending
        self._active_subscriptions.confirmation_started.set()
        try:
            async with asyncio.timeout_at(pending.deadline):
                await connection.write_frame(
                    SubscribeFrame.build(
                        subscription_id=self.id,
                        destination=self.destination,
                        ack=self.ack,
                        headers={**(self.headers or {}), "receipt": pending.receipt_id},
                    )
                )
        except TimeoutError as error:
            failure = SubscriptionError(subscription_id=self.id, reason="timeout")
            self._fail_confirmation(pending, failure, include_confirmed=True)
            # A receipt can arrive before the socket write finishes draining.
            # A failed subscribe must not leave that already-confirmed entry
            # behind without a subscription handle for the caller to close.
            await self._cleanup_confirmation(pending)
            raise failure from error
        except BaseException as error:
            self._fail_confirmation(
                pending,
                SubscriptionError(
                    subscription_id=self.id,
                    reason=(
                        "unsubscribed"
                        if isinstance(error, asyncio.CancelledError) and not restoring
                        else "connection_lost"
                    ),
                ),
                notify=restoring or not isinstance(error, asyncio.CancelledError),
                include_confirmed=True,
            )
            await self._cleanup_confirmation(pending)
            raise
        return pending

    async def _await_confirmation(self, pending: _PendingConfirmation) -> None:
        try:
            if pending.result.done():
                error = pending.result.result()
            else:
                async with asyncio.timeout_at(pending.deadline):
                    error = await asyncio.shield(pending.result)
        except TimeoutError as timeout_error:
            # Prefer a receipt already processed by the reader over a deadline
            # cancellation delivered before this waiter was rescheduled.
            if pending.result.done():
                error = pending.result.result()
                if error is not None:
                    raise error from timeout_error
                return
            error = SubscriptionError(subscription_id=self.id, reason="timeout")
            self._fail_confirmation(pending, error)
            await self._cleanup_confirmation(pending)
            raise error from timeout_error
        except asyncio.CancelledError:
            was_active = self._active_subscriptions.contains_by_id(self.id)
            pending = self._pending_confirmation or pending
            self._fail_confirmation(
                pending, SubscriptionError(subscription_id=self.id, reason="unsubscribed"), notify=False
            )
            # A receipt may have arrived just before cancellation.
            self._active_subscriptions.delete_by_id(self.id)
            if was_active:
                await self._cleanup_confirmation(pending)
            raise
        if error is not None:
            raise error

    def _fail_confirmation(
        self,
        pending: _PendingConfirmation,
        error: SubscriptionError,
        *,
        notify: bool = True,
        include_confirmed: bool = False,
    ) -> None:
        if self._active_subscriptions.pending_receipts.pop(pending.receipt_id, None) is None:
            if not include_confirmed:
                return
            # A write may fail after its receipt. Notify once, without removing
            # a subscription already being restored on a newer connection.
            if (
                self._pending_confirmation is not None
                or not pending.result.done()
                or pending.result.result() is not None
                or not self._active_subscriptions.contains_by_id(self.id)
            ):
                return
        if not self._active_subscriptions.pending_receipts:
            self._active_subscriptions.confirmation_started.clear()
        self._pending_confirmation = None
        self._active_subscriptions.delete_by_id(self.id)
        if not pending.result.done():
            pending.result.set_result(error)
        if notify:
            if self.on_subscription_error is None:
                LOGGER.warning("subscription confirmation failed: %s", error)
            else:
                try:
                    self.on_subscription_error(error)
                except Exception:  # ruff: ignore[blind-except] - user callbacks must not terminate the frame reader
                    LOGGER.exception("unhandled exception in subscription error callback")

    async def _cleanup_confirmation(self, pending: _PendingConfirmation) -> None:
        try:
            async with asyncio.timeout(self.receipt_timeout):
                await pending.connection.write_frame(UnsubscribeFrame(headers={"id": self.id}))
        except (ConnectionLostError, TimeoutError) as error:
            state = self._connection_manager._active_connection_state
            if state is not None and state.connection is pending.connection:
                await self._connection_manager._discard_failed_connection_state(
                    state, ConnectionLostError(reason=error)
                )

    async def _watch_confirmation(self, pending: _PendingConfirmation) -> None:
        # Failure notification and registry cleanup happen before waking us.
        with suppress(SubscriptionError):
            await self._await_confirmation(pending)

    async def _resubscribe(self, connection: AbstractConnection) -> None:
        if not self._active_subscriptions.contains_by_id(self.id):
            return
        if self._pending_confirmation is not None:
            return
        if self.receipt_timeout is None:
            await connection.write_frame(
                SubscribeFrame.build(
                    subscription_id=self.id, destination=self.destination, ack=self.ack, headers=self.headers
                )
            )
        else:
            try:
                pending = await self._send_confirmed_subscription(connection, restoring=True)
            except SubscriptionError:
                return
            self._active_subscriptions.watch_confirmation(pending)

    async def _nack(self, frame: MessageFrame, *, received_at_reconnection_count: int) -> None:
        if not self._active_subscriptions.contains_by_id(self.id):
            LOGGER.warning(
                "failed to nack message frame: subscription is not active. "
                "message_id: %s, subscription_id: %s, active_subscriptions: %s",
                frame.headers["message-id"],
                self.id,
                self._active_subscriptions.get_ids(),
            )
            return
        if not (ack_id := frame.headers.get("ack")):
            LOGGER.warning(
                'failed to nack message frame: it has no "ack" header. "'
                "message_id: %s, subscription_id: %s, frame_header_names: %s",
                frame.headers["message-id"],
                self.id,
                frame.headers.keys(),
            )
            return
        if received_at_reconnection_count != self._connection_manager._reconnection_count:
            LOGGER.error(
                "skipping nack for message frame: connection changed since message was received. "
                "message_id: %s, subscription_id: %s, received_at_reconnection_count: %s, "
                "current_reconnection_count: %s",
                frame.headers["message-id"],
                self.id,
                received_at_reconnection_count,
                self._connection_manager._reconnection_count,
            )
            return
        await self._connection_manager.maybe_write_frame(NackFrame(headers={"id": ack_id, "subscription": self.id}))

    async def _ack(self, frame: MessageFrame, *, received_at_reconnection_count: int) -> None:
        if not self._active_subscriptions.contains_by_id(self.id):
            LOGGER.warning(
                "failed to ack message frame: subscription is not active. "
                "message_id: %s, subscription_id: %s, active_subscriptions: %s",
                frame.headers["message-id"],
                self.id,
                self._active_subscriptions.get_ids(),
            )
            return
        if not (ack_id := frame.headers.get("ack")):
            LOGGER.warning(
                'failed to ack message frame: it has no "ack" header. "'
                "message_id: %s, subscription_id: %s, frame_header_names: %s",
                frame.headers["message-id"],
                self.id,
                frame.headers.keys(),
            )
            return
        if received_at_reconnection_count != self._connection_manager._reconnection_count:
            LOGGER.warning(
                "skipping ack for message frame: connection changed since message was received. "
                "message_id: %s, subscription_id: %s, received_at_reconnection_count: %s, "
                "current_reconnection_count: %s",
                frame.headers["message-id"],
                self.id,
                received_at_reconnection_count,
                self._connection_manager._reconnection_count,
            )
            return
        await self._connection_manager.maybe_write_frame(AckFrame(headers={"id": ack_id, "subscription": self.id}))


@dataclass(kw_only=True, slots=True)
class AutoAckSubscription(BaseSubscription):
    handler: Callable[[MessageFrame], Awaitable[Any]]
    on_suppressed_exception: Callable[[Exception, MessageFrame], Any]
    suppressed_exception_classes: tuple[type[Exception], ...]
    _should_handle_ack_nack: bool = field(init=False)

    def __post_init__(self) -> None:
        self._should_handle_ack_nack = self.ack in {"client", "client-individual"}

    async def _run_handler(self, *, frame: MessageFrame, received_at_reconnection_count: int) -> None:
        try:
            await self.handler(frame)
        except self.suppressed_exception_classes as exception:
            if self._should_handle_ack_nack:
                await self._nack(frame, received_at_reconnection_count=received_at_reconnection_count)
            self.on_suppressed_exception(exception, frame)
        else:
            if self._should_handle_ack_nack:
                await self._ack(frame, received_at_reconnection_count=received_at_reconnection_count)


@dataclass(kw_only=True, slots=True)
class ManualAckSubscription(BaseSubscription):
    handler: Callable[["AckableMessageFrame"], Coroutine[Any, Any, Any]]


@dataclass(frozen=True, kw_only=True, slots=True)
class AckableMessageFrame(MessageFrame):
    _subscription: ManualAckSubscription
    _received_at_reconnection_count: int

    async def ack(self) -> None:
        await self._subscription._ack(self, received_at_reconnection_count=self._received_at_reconnection_count)

    async def nack(self) -> None:
        await self._subscription._nack(self, received_at_reconnection_count=self._received_at_reconnection_count)


def _make_subscription_id() -> str:
    return str(uuid4())


def _make_confirmation_id() -> str:
    return str(uuid4())


async def resubscribe_to_active_subscriptions(
    *, connection: AbstractConnection, active_subscriptions: ActiveSubscriptions
) -> None:
    for subscription in active_subscriptions.get_all():
        await subscription._resubscribe(connection)


async def unsubscribe_from_all_active_subscriptions(*, active_subscriptions: ActiveSubscriptions) -> None:
    for subscription in active_subscriptions.get_all():
        await subscription.unsubscribe()
