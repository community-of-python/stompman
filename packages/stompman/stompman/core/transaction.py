"""A transaction owns its journal; recovery only sees the Restorable interface."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Literal, Self, cast

from ._tasks import await_cleanup
from .command import Command
from .config import Confirmation, Unconfirmed
from .errors import ConnectionLostError, ReceiptRejectedError, ReceiptTimeoutError, TransactionOutcomeUnknownError
from .frames import AbortFrame, BeginFrame, CommitFrame, SendFrame, SendHeaders
from .recovery import ConnectionSupervisor
from .session import Session


class TransactionState(StrEnum):
    NEW = "new"
    OPEN = "open"
    COMMIT_REQUESTED = "commit-requested"
    COMPLETED = "completed"
    ABORTED = "aborted"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True, eq=False)
class JournalEntry:
    headers: tuple[tuple[str, str], ...]
    body: bytes

    @classmethod
    def from_frame(cls, frame: SendFrame) -> Self:
        return cls(tuple(cast("dict[str, str]", frame.headers).items()), frame.body)

    def frame(self) -> SendFrame:
        return SendFrame(headers=cast("SendHeaders", dict(self.headers)), body=self.body)


@dataclass(frozen=True, slots=True)
class Open:
    journal: list[JournalEntry]

    def discard(self, entry: JournalEntry) -> None:
        with suppress(ValueError):
            self.journal.remove(entry)


@dataclass(frozen=True, slots=True)
class Committing:
    journal: tuple[JournalEntry, ...]
    session: Session


@dataclass(frozen=True, slots=True)
class Finished:
    outcome: Literal[TransactionState.COMPLETED, TransactionState.ABORTED, TransactionState.UNCERTAIN]
    journal: tuple[JournalEntry, ...]


class Transaction:
    def __init__(
        self,
        connections: ConnectionSupervisor,
        transaction_id: str,
        confirmation: Confirmation,
        commit_confirmation: Confirmation,
        *,
        replay_source: Callable[[], tuple[SendFrame, ...]] | None = None,
    ) -> None:
        if not transaction_id:
            msg = "transaction id must not be empty"
            raise ValueError(msg)
        self._id = transaction_id
        self._state: Literal[TransactionState.NEW] | Open | Committing | Finished = TransactionState.NEW
        self._connections = connections
        self._confirmation = confirmation
        self._commit_confirmation = commit_confirmation
        self._lock = asyncio.Lock()
        self._replay: Callable[[], tuple[JournalEntry, ...]]
        if replay_source is None:
            self._replay = lambda: self._journal
        else:
            self._replay = lambda: tuple(JournalEntry.from_frame(frame) for frame in replay_source())

    @property
    def id(self) -> str:
        return self._id

    @property
    def state(self) -> TransactionState:
        match self._state:
            case Open():
                return TransactionState.OPEN
            case Committing():
                return TransactionState.COMMIT_REQUESTED
            case Finished(outcome=outcome):
                return outcome
            case TransactionState.NEW:
                return TransactionState.NEW

    @property
    def _journal(self) -> tuple[JournalEntry, ...]:
        return () if self._state is TransactionState.NEW else tuple(self._state.journal)

    @property
    def key(self) -> tuple[str, str]:
        return "transaction", self.id

    @property
    def sent_frames(self) -> list[SendFrame]:
        return [entry.frame() for entry in self._journal]

    def _require_open(self) -> Open:
        if isinstance(self._state, Open):
            return self._state
        msg = f"transaction is {self.state.value}"
        raise RuntimeError(msg)

    async def __aenter__(self) -> Self:
        async with self._lock:
            if self.state is not TransactionState.NEW:
                msg = "transaction has already been started"
                raise RuntimeError(msg)

            async def begin(session: Session) -> Command:
                self._connections.attach(self)
                try:
                    command = await session.submit(BeginFrame(headers={"transaction": self.id}), self._confirmation)
                except BaseException:
                    self._connections.detach(self)
                    raise
                self._state = Open([])
                return command

            command = await self._connections.run(begin, attempts=self._confirmation.attempts)
            try:
                await command.complete()
            except BaseException:
                self.retire()
                command.invalidate("BEGIN was not confirmed")
                raise
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        if exc_type is None:
            await self.commit()
        else:
            with suppress(ConnectionLostError, ReceiptRejectedError, ReceiptTimeoutError):
                await self.abort()

    async def send(
        self,
        body: bytes,
        destination: str,
        *,
        content_type: str | None = None,
        add_content_length: bool = True,
        headers: dict[str, str] | None = None,
    ) -> None:
        frame = SendFrame.build(
            body=body,
            destination=destination,
            transaction=self.id,
            content_type=content_type,
            add_content_length=add_content_length,
            headers=headers,
        )
        async with self._lock:
            opened = self._require_open()
            entry = JournalEntry.from_frame(frame)

            async def submit(session: Session) -> Command:
                # The generation gate makes recording and draining one operation.
                # A rejection removes its exact entry synchronously in the reader,
                # before recovery can replay it, even when it arrives before drain.
                opened.journal.append(entry)
                try:
                    command = session.command(frame, self._confirmation, lambda _error: opened.discard(entry))
                    await command.submit()
                except BaseException:
                    opened.discard(entry)
                    raise
                return command

            command = await self._connections.run(submit, attempts=self._confirmation.attempts)
            await command.complete()

    async def restore(self, session: Session) -> None:
        if isinstance(self._state, Open):
            journal = self._replay()
            await session.write(BeginFrame(headers={"transaction": self.id}), Unconfirmed())
            for entry in journal:
                await session.write(entry.frame(), Unconfirmed())

    @staticmethod
    def disconnected(session: Session) -> None:
        del session

    def retire(self) -> None:
        self._state = Finished(TransactionState.ABORTED, self._journal)
        self._connections.detach(self)

    async def commit(self) -> None:
        async with self._lock:
            opened = self._require_open()

            async def submit(session: Session) -> Command:
                # Once COMMIT may reach the wire, this journal can never replay.
                self._connections.detach(self)
                self._state = Committing(tuple(opened.journal), session)
                return await session.submit(CommitFrame(headers={"transaction": self.id}), self._commit_confirmation)

            try:
                command = await self._connections.run(submit)
                await command.complete()
            except BaseException as error:
                self._commit_failed(error)
                raise
            self._state = Finished(TransactionState.COMPLETED, tuple(opened.journal))

    def _commit_failed(self, error: BaseException) -> None:
        state = self._state
        if isinstance(state, Committing):
            self._state = Finished(TransactionState.UNCERTAIN, state.journal)
            if isinstance(error, asyncio.CancelledError):
                state.session.fail(ConnectionLostError(reason="commit was cancelled"))
            elif isinstance(error, Exception):
                raise TransactionOutcomeUnknownError(transaction_id=self.id, reason=error) from error
        elif not isinstance(error, asyncio.CancelledError):
            self.retire()

    async def abort(self) -> None:
        async with self._lock:
            if self.state is not TransactionState.OPEN:
                return
            # Withdraw replay intent before waiting for a generation. Once abort
            # is requested, its cleanup remains owned even if the caller cancels.
            self.retire()
            await await_cleanup(self._abort_current())

    async def _abort_current(self) -> None:
        with suppress(ConnectionLostError):
            await self._connections.write_current(AbortFrame(headers={"transaction": self.id}), self._confirmation)
