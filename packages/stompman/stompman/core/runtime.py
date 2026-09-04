# Task entrypoints retain broad cleanup scopes so every failure reaches the lifecycle owner.
import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from types import TracebackType
from typing import Any, Literal, Self, overload
from uuid import uuid4

from stompman.config import ConnectionParameters, Heartbeat
from stompman.core._tasks import await_cleanup
from stompman.core.config import RuntimeConfig
from stompman.core.delivery import Delivery, PendingDelivery, Subscription
from stompman.core.session import Session
from stompman.core.transaction import Transaction, TransactionState
from stompman.errors import (
    AllServersUnavailable,
    AnyConnectionIssue,
    ConnectionLostError,
    ConnectionLostOnLifespanEnter,
    ConsumerOverloadedError,
    FailedAllConnectAttemptsError,
    FailedAllWriteAttemptsError,
)
from stompman.frames import (
    AckFrame,
    AckMode,
    AnyClientFrame,
    AnyServerFrame,
    ErrorFrame,
    MessageFrame,
    NackFrame,
    ReceiptFrame,
    SendFrame,
    UnsubscribeFrame,
)
from stompman.logger import LOGGER


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    state: Literal["closed", "connecting", "connected", "recovering", "failed"]
    generation: int
    heartbeat: Heartbeat
    pending_messages: int
    pending_bytes: int
    running_handlers: int
    failure: Exception | None


class Runtime:
    """The shared execution seam for adapters; owns recovery and delivery state."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.config = config
        self._session: Session | None = None
        self._generation = 0
        self._lock = asyncio.Lock()
        self._lifecycle = asyncio.Lock()
        self._opened = False
        self._closing = False
        self._failure: Exception | None = None
        self._subscriptions: dict[str, Subscription] = {}
        self._empty = asyncio.Event()
        self._empty.set()
        self._transactions: dict[str, Transaction] = {}
        self._queue: deque[PendingDelivery] = deque()
        self._available = asyncio.Event()
        self._pending: set[PendingDelivery] = set()
        self._pending_bytes = 0
        self._handlers: set[asyncio.Task[None]] = set()
        self._group: asyncio.TaskGroup | None = None
        self._supervisor: asyncio.Task[None] | None = None
        self._dispatcher: asyncio.Task[None] | None = None
        self._slots: asyncio.Semaphore | None = None

    @property
    def status(self) -> RuntimeStatus:
        state: Literal["closed", "connecting", "connected", "recovering", "failed"]
        if self._failure is not None:
            state = "failed"
        elif not self._opened:
            state = "closed"
        elif self._session is not None and not self._session.failed.is_set():
            state = "connected"
        else:
            state = "recovering" if self._generation else "connecting"
        return RuntimeStatus(
            state=state,
            generation=self._generation,
            heartbeat=self._session.heartbeat if self._session else Heartbeat(0, 0),
            pending_messages=len(self._pending),
            pending_bytes=self._pending_bytes,
            running_handlers=len(self._handlers),
            failure=self._failure,
        )

    def is_alive(self) -> bool:
        return bool(
            self._opened
            and not self._closing
            and self._failure is None
            and self._session is not None
            and self._session.is_alive()
        )

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self.close(exc_type, exc_value, traceback)

    async def start(self) -> None:
        async with self._lifecycle:
            if self._opened:
                return
            self.config.validate()
            self._failure = None
            self._closing = False
            self._opened = True
            try:  # ruff: ignore[too-many-statements-in-try-clause]
                async with self._lock:
                    await self._ensure_session()
                self._group = asyncio.TaskGroup()
                await self._group.__aenter__()
                self._slots = (
                    asyncio.Semaphore(self.config.max_concurrent_handlers)
                    if self.config.max_concurrent_handlers is not None
                    else None
                )
                self._supervisor = self._group.create_task(self._supervise(), name="stomp-recovery")
                self._dispatcher = self._group.create_task(self._dispatch(), name="stomp-delivery")
            except BaseException:
                self._opened = False
                if self._session is not None:
                    await self._session.close()
                    self._session = None
                raise

    async def close(
        self,
        exc_type: type[BaseException] | None = None,  # ruff: ignore[unused-method-argument]
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,  # ruff: ignore[unused-method-argument]
        *,
        cancel_handlers: bool = False,
    ) -> None:
        async with self._lifecycle:
            if not self._opened:
                return
            self._closing = True
            if self._dispatcher is not None:
                self._dispatcher.cancel()
            for subscription in self._subscriptions.values():
                self.pause(subscription)
            try:
                if cancel_handlers or exc_value is not None or self._failure is not None:
                    for task in self._handlers:
                        task.cancel()
                await asyncio.gather(*self._handlers, return_exceptions=True)
                for subscription in list(self._subscriptions.values()):
                    await self.unsubscribe(subscription)
            finally:
                await self._finish_close(graceful=exc_value is None and self._failure is None)

    async def _finish_close(self, *, graceful: bool) -> None:
        self._opened = False
        for background_task in (self._supervisor, self._dispatcher):
            if background_task is not None:
                background_task.cancel()
        try:
            if self._group is not None:
                await self._group.__aexit__(None, None, None)
        finally:
            try:
                if self._session is not None:
                    await self._session.close(graceful=graceful)
            finally:
                self._session = None
                for subscription in list(self._subscriptions.values()):
                    self._remove_subscription(subscription)
                for transaction in self._transactions.values():
                    transaction.state = TransactionState.ABORTED
                self._transactions.clear()
                self._group = None
                self._closing = False

    async def wait_until_unsubscribed(self) -> None:
        await self._empty.wait()

    def _require_open(self) -> None:
        if not self._opened or (self._closing and asyncio.current_task() not in self._handlers):
            msg = "runtime is not running"
            raise RuntimeError(msg)
        if self._failure is not None:
            raise self._failure

    async def _candidate(self, server: ConnectionParameters) -> Session | AnyConnectionIssue:
        try:
            connection = await self.config.connection_class.connect(
                host=server.host,
                port=server.port,
                timeout=self.config.connect_timeout,
                read_max_chunk_size=self.config.read_max_chunk_size,
                ssl=self.config.ssl,
                ws_uri_path=server.ws_uri_path,
            )
        except OSError:
            return AllServersUnavailable(servers=[server], timeout=self.config.connect_timeout)
        if connection is None:
            return AllServersUnavailable(servers=[server], timeout=self.config.connect_timeout)
        session = Session(connection, server, self.config)
        successful = False
        try:
            try:
                issue = await session.handshake()
            except (ConnectionLostError, OSError, ValueError):
                return ConnectionLostOnLifespanEnter()
            if issue is not None:
                return issue
            successful = True
            return session
        finally:
            if not successful:
                await session.close()

    async def _race(self) -> tuple[Session | None, list[AnyConnectionIssue]]:
        tasks = [asyncio.create_task(self._candidate(server)) for server in self.config.servers]
        winner = None
        issues = []

        async def cleanup(keep: Session | None) -> None:
            for task in tasks:
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            sessions = [result for result in results if isinstance(result, Session) and result is not keep]
            close_results = await asyncio.gather(*(session.close() for session in sessions), return_exceptions=True)
            for result in close_results:
                if isinstance(result, BaseException):
                    raise result

        try:  # ruff: ignore[too-many-statements-in-try-clause]
            for completed in asyncio.as_completed(tasks):
                result = await completed
                if isinstance(result, Session):
                    winner = result
                    break
                issues.append(result)
            await await_cleanup(asyncio.create_task(cleanup(winner)))
        except BaseException:
            # Ownership transfers only after all loser cleanup succeeds.
            await await_cleanup(asyncio.create_task(cleanup(None)))
            raise
        return winner, issues

    async def _ensure_session(self) -> Session:
        """Restore state under the operation lock before exposing a generation."""
        if self._session is not None and not self._session.failed.is_set():
            return self._session
        if (
            self._session is not None
            and self._session.failure is not None
            and not isinstance(self._session.failure, (ConnectionLostError, OSError))
        ):
            raise self._session.failure
        await self._discard_session()
        issues: list[AnyConnectionIssue] = []
        for attempt in range(self.config.connect_retry_attempts):
            session, attempt_issues = await self._race()
            issues.extend(attempt_issues)
            if session is not None:
                self._generation += 1
                session.generation = self._generation
                self._session = session
                session.start(self._receive)
                try:
                    for subscription in self._subscriptions.values():
                        await session.write(subscription.frame())
                    for transaction in self._transactions.values():
                        await transaction.restore(session)
                except ConnectionLostError:
                    issues.append(ConnectionLostOnLifespanEnter())
                    await self._discard_session()
                else:
                    return session
            if attempt + 1 < self.config.connect_retry_attempts:
                await asyncio.sleep(self.config.connect_retry_interval * (attempt + 1))
        raise FailedAllConnectAttemptsError(retry_attempts=self.config.connect_retry_attempts, issues=issues)

    async def _discard_session(self) -> None:
        session = self._session
        self._session = None
        if session is not None:
            for pending in list(self._pending):
                if pending.delivery._generation == session.generation:
                    pending.wire_done = True
                    if pending.handler_done:
                        self._release(pending)
            self._drop_queued(lambda pending: pending.delivery._generation == session.generation)
            for subscription in self._subscriptions.values():
                subscription._unsettled.clear()
            await session.close()

    async def reconnect(self) -> None:
        """Replace the session and restore desired subscriptions and open transactions."""
        async with self._lock:
            self._require_open()
            await self._discard_session()
            await self._ensure_session()

    async def _supervise(self) -> None:
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            while self._opened:
                session = self._session
                if session is not None:
                    await session.failed.wait()
                    if session.failure is not None and not isinstance(session.failure, (ConnectionLostError, OSError)):
                        raise session.failure  # ruff: ignore[raise-within-try]
                try:
                    async with self._lock:
                        if self._closing:
                            return
                        await self._ensure_session()
                except FailedAllConnectAttemptsError:
                    if not self.config.keep_alive_on_connection_failure:
                        raise
                    LOGGER.warning("background recovery exhausted; keeping runtime alive")
                    await asyncio.sleep(self.config.connect_retry_interval)
        except Exception as error:
            self._failure = error
            raise

    async def _write(self, frame: AnyClientFrame, *, receipt_timeout: float | None = None) -> ReceiptFrame | None:
        for _ in range(self.config.write_retry_attempts):
            session = await self._ensure_session()
            try:
                return await session.write(frame, receipt_timeout=receipt_timeout)
            except ConnectionLostError:
                await self._discard_session()
                if receipt_timeout is not None:
                    raise
        raise FailedAllWriteAttemptsError(retry_attempts=self.config.write_retry_attempts)

    @overload
    async def send(
        self,
        body: bytes,
        destination: str,
        *,
        content_type: str | None = None,
        add_content_length: bool = True,
        headers: dict[str, str] | None = None,
        receipt_timeout: None = None,
    ) -> None: ...

    @overload
    async def send(
        self,
        body: bytes,
        destination: str,
        *,
        content_type: str | None = None,
        add_content_length: bool = True,
        headers: dict[str, str] | None = None,
        receipt_timeout: float,
    ) -> ReceiptFrame: ...

    async def send(
        self,
        body: bytes,
        destination: str,
        *,
        content_type: str | None = None,
        add_content_length: bool = True,
        headers: dict[str, str] | None = None,
        receipt_timeout: float | None = None,
    ) -> ReceiptFrame | None:
        frame = SendFrame.build(
            body=body,
            destination=destination,
            transaction=None,
            content_type=content_type,
            add_content_length=add_content_length,
            headers=headers,
        )
        async with self._lock:
            self._require_open()
            return await self._write(frame, receipt_timeout=receipt_timeout)

    def begin(self, *, receipt_timeout: float | None = None, transaction_id: str | None = None) -> Transaction:
        return Transaction(self, receipt_timeout=receipt_timeout, transaction_id=transaction_id)

    async def subscribe(
        self,
        destination: str,
        handler: Callable[[Delivery], Awaitable[Any]],
        *,
        ack: AckMode = "client-individual",
        headers: dict[str, str] | None = None,
        subscription_id: str | None = None,
    ) -> Subscription:
        subscription = Subscription(
            id=subscription_id or str(uuid4()),
            destination=destination,
            ack=ack,
            headers=headers.copy() if headers is not None else None,
            handler=handler,
            _runtime=self,
        )
        async with self._lock:
            self._require_open()
            if subscription.id in self._subscriptions:
                msg = "subscription id is already active"
                raise ValueError(msg)
            # Register before writing: the broker can deliver immediately after SUBSCRIBE.
            # Each attempt establishes the session before installing this new intent.
            for _ in range(self.config.write_retry_attempts):
                session = await self._ensure_session()
                self._subscriptions[subscription.id] = subscription
                self._empty.clear()
                try:
                    await session.write(subscription.frame())
                except BaseException as error:
                    self._remove_subscription(subscription)
                    if not isinstance(error, ConnectionLostError):
                        raise
                    await self._discard_session()
                else:
                    return subscription
            raise FailedAllWriteAttemptsError(retry_attempts=self.config.write_retry_attempts)

    def _remove_subscription(self, subscription: Subscription) -> None:
        self._subscriptions.pop(subscription.id, None)
        if not self._subscriptions:
            self._empty.set()
        for pending in list(subscription._deliveries.values()):
            pending.wire_done = True
            if pending.handler_done:
                self._release(pending)
        subscription._unsettled.clear()
        self._drop_queued(lambda pending: pending.delivery._subscription is subscription)

    async def unsubscribe(self, subscription: Subscription) -> None:
        async with self._lock:
            if self._subscriptions.get(subscription.id) is not subscription:
                return
            self._remove_subscription(subscription)
            if self._session is not None and not self._session.failed.is_set():
                with suppress(ConnectionLostError):
                    await self._session.write(UnsubscribeFrame(headers={"id": subscription.id}))

    def pause(self, subscription: Subscription) -> None:
        subscription._paused = True
        self._drop_queued(lambda pending: pending.delivery._subscription is subscription)

    def _receive(self, frame: AnyServerFrame, session: Session) -> None:
        if isinstance(frame, ErrorFrame):
            if self.config.on_error_frame is not None:
                self.config.on_error_frame(frame)
            session.fail(ConnectionLostError(reason="broker sent ERROR"))
        elif isinstance(frame, MessageFrame) and not self._closing:
            subscription = self._subscriptions.get(frame.headers["subscription"])
            if subscription is None or subscription._paused:
                return
            size = len(frame.body) + sum(
                len(key.encode()) + len(str(value).encode()) for key, value in frame.headers.items()
            )
            if len(self._pending) >= self.config.max_pending_messages or (
                self._pending_bytes + size > self.config.max_pending_bytes
            ):
                raise ConsumerOverloadedError(
                    max_pending_messages=self.config.max_pending_messages,
                    max_pending_bytes=self.config.max_pending_bytes,
                )
            sequence = subscription._next_sequence
            subscription._next_sequence += 1
            delivery = Delivery(
                headers=frame.headers.copy(),
                body=frame.body,
                _subscription=subscription,
                _generation=session.generation,
                _sequence=sequence,
            )
            pending = PendingDelivery(delivery=delivery, size=size, wire_done=subscription.ack == "auto")
            subscription._deliveries[sequence] = pending
            if subscription.ack == "client":
                subscription._unsettled.append(pending)
            self._pending.add(pending)
            self._pending_bytes += size
            self._queue.append(pending)
            self._available.set()

    def _release(self, pending: PendingDelivery) -> None:
        if pending in self._pending:
            self._pending.remove(pending)
            self._pending_bytes -= pending.size
            pending.delivery._subscription._deliveries.pop(pending.delivery._sequence, None)

    def _drop_queued(self, predicate: Callable[[PendingDelivery], bool]) -> None:
        remaining: deque[PendingDelivery] = deque()
        for pending in self._queue:
            if predicate(pending):
                self._release(pending)
            else:
                remaining.append(pending)
        self._queue = remaining
        if not self._queue:
            self._available.clear()

    async def _dispatch(self) -> None:
        while True:
            await self._available.wait()
            if self._slots is not None:
                await self._slots.acquire()
            if not self._queue:
                if self._slots is not None:
                    self._slots.release()
                continue
            pending = self._queue.popleft()
            if not self._queue:
                self._available.clear()
            assert self._group is not None  # ruff: ignore[assert]
            task = self._group.create_task(self._run_handler(pending), name="stomp-handler")
            self._handlers.add(task)
            task.add_done_callback(partial(self._handler_finished, pending=pending))

    def _handler_finished(self, task: asyncio.Task[None], *, pending: PendingDelivery) -> None:
        pending.handler_done = True
        if pending.wire_done:
            self._release(pending)
        self._handlers.discard(task)
        if self._slots is not None:
            self._slots.release()

    async def _run_handler(self, pending: PendingDelivery) -> None:
        try:
            await pending.delivery._subscription.handler(pending.delivery)
        except Exception:  # ruff: ignore[blind-except]
            LOGGER.exception("unhandled exception in message handler")
        finally:
            pending.handler_done = True
            if pending.wire_done:
                self._release(pending)

    async def settle(self, delivery: Delivery, *, accepted: bool) -> None:
        async with self._lock:
            subscription = delivery._subscription
            pending = subscription._deliveries.get(delivery._sequence)
            if pending is None or pending.wire_done:
                return
            session = self._session
            if (
                self._subscriptions.get(subscription.id) is not subscription
                or session is None
                or session.failed.is_set()
                or session.generation != delivery._generation
            ):
                LOGGER.warning("skipping settlement from an inactive subscription or session")
                pending.wire_done = True
                if pending.handler_done:
                    self._release(pending)
                return
            if pending.outcome is not None:
                return
            pending.outcome = accepted
            if subscription.ack == "client":
                while subscription._unsettled and subscription._unsettled[0].outcome is not None:
                    await self._settle_one(subscription._unsettled.popleft(), session)
            else:
                await self._settle_one(pending, session)

    async def _settle_one(self, pending: PendingDelivery, session: Session) -> None:
        delivery = pending.delivery
        ack_id = delivery.headers.get("ack")
        if ack_id and not session.failed.is_set():
            frame_type = AckFrame if pending.outcome else NackFrame
            with suppress(ConnectionLostError):
                await session.write(frame_type(headers={"id": ack_id, "subscription": delivery._subscription.id}))
        elif not ack_id:
            LOGGER.warning("failed to settle message frame: it has no ack header")
        pending.wire_done = True
        if pending.handler_done:
            self._release(pending)
