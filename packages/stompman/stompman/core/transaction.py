"""A transaction owns its journal; recovery only sees the Restorable interface."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import Self, cast

from .config import Confirmation, Unconfirmed
from .errors import ConnectionLostError, ReceiptRejectedError, ReceiptTimeoutError, TransactionOutcomeUnknownError
from .frames import AbortFrame, BeginFrame, CommitFrame, SendFrame, SendHeaders
from .recovery import ConnectionSupervisor
from .session import Command, Session


class TransactionState(StrEnum):
    NEW = "new"
    OPEN = "open"
    COMMIT_REQUESTED = "commit-requested"
    COMPLETED = "completed"
    ABORTED = "aborted"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class JournalEntry:
    headers: tuple[tuple[str, str], ...]
    body: bytes

    @classmethod
    def from_frame(cls, frame: SendFrame) -> Self:
        return cls(tuple(cast("dict[str, str]", frame.headers).items()), frame.body)

    def frame(self) -> SendFrame:
        return SendFrame(headers=cast("SendHeaders", dict(self.headers)), body=self.body)


class Transaction:
    def __init__(
        self,
        connections: ConnectionSupervisor,
        transaction_id: str,
        confirmation: Confirmation,
        commit_confirmation: Confirmation,
    ) -> None:
        if not transaction_id:
            msg = "transaction id must not be empty"
            raise ValueError(msg)
        self.id = transaction_id
        self._state = TransactionState.NEW
        self._connections = connections
        self._confirmation = confirmation
        self._commit_confirmation = commit_confirmation
        self._journal: list[JournalEntry] = []
        self._lock = asyncio.Lock()

    @property
    def state(self) -> TransactionState:
        return self._state

    @property
    def key(self) -> tuple[str, str]:
        return "transaction", self.id

    @property
    def sent_frames(self) -> list[SendFrame]:
        return [entry.frame() for entry in self._journal]

    def _require_open(self) -> None:
        if self.state is not TransactionState.OPEN:
            msg = f"transaction is {self.state.value}"
            raise RuntimeError(msg)

    @property
    def _attempts(self) -> int:
        return self._confirmation.attempts if isinstance(self._confirmation, Unconfirmed) else 1

    @staticmethod
    async def _complete(command: Command) -> None:
        try:
            await command.complete()
        finally:
            command.close()

    async def __aenter__(self) -> Self:
        async with self._lock:
            if self.state is not TransactionState.NEW:
                msg = "transaction has already been started"
                raise RuntimeError(msg)

            async def begin(session: Session) -> Command:
                self._connections.attach(self)
                command = session.command(BeginFrame(headers={"transaction": self.id}), self._confirmation)
                try:
                    await command.submit()
                except BaseException:
                    self._connections.detach(self)
                    command.close()
                    raise
                self._state = TransactionState.OPEN
                return command

            command = await self._connections.run(begin, attempts=self._attempts)
            try:
                await self._complete(command)
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
            self._require_open()

            async def submit(session: Session) -> Command:
                command = session.command(frame, self._confirmation)
                try:
                    await command.submit()
                except BaseException:
                    command.close()
                    raise
                # Journal insertion and submission share the generation lock. Recovery
                # can never replay this triggering SEND and then submit it a second time.
                self._journal.append(JournalEntry.from_frame(frame))
                return command

            command = await self._connections.run(submit, attempts=self._attempts)
            await self._complete(command)

    async def restore(self, session: Session) -> None:
        if self.state is TransactionState.OPEN:
            await session.write(BeginFrame(headers={"transaction": self.id}), Unconfirmed())
            for entry in self._journal:
                await session.write(entry.frame(), Unconfirmed())

    @staticmethod
    def disconnected(session: Session) -> None:
        del session

    def retire(self) -> None:
        self._state = TransactionState.ABORTED
        self._connections.detach(self)

    async def commit(self) -> None:
        async with self._lock:
            self._require_open()

            async def submit(session: Session) -> Command:
                # Exclude this transaction from recovery before COMMIT can reach the wire.
                self._connections.detach(self)
                self._state = TransactionState.COMMIT_REQUESTED
                command = session.command(CommitFrame(headers={"transaction": self.id}), self._commit_confirmation)
                try:
                    await command.submit()
                except BaseException:
                    command.close()
                    raise
                return command

            try:  # ruff: ignore[too-many-statements-in-try-clause]
                command = await self._connections.run(submit)
                try:
                    await self._complete(command)
                except asyncio.CancelledError:
                    command.invalidate("commit was cancelled")
                    raise
            except BaseException as error:
                if self.state is TransactionState.OPEN:
                    if not isinstance(error, asyncio.CancelledError):
                        self.retire()
                    raise
                self._state = TransactionState.UNCERTAIN
                if isinstance(error, asyncio.CancelledError):
                    raise
                if isinstance(error, Exception):
                    raise TransactionOutcomeUnknownError(transaction_id=self.id, reason=error) from error
                raise
            else:
                self._state = TransactionState.COMPLETED

    async def abort(self) -> None:
        async with self._lock:
            if self.state is not TransactionState.OPEN:
                return

            try:
                command = await self._connections.submit_current(
                    AbortFrame(headers={"transaction": self.id}), self._confirmation
                )
            except ConnectionLostError:
                self.retire()
            else:
                self.retire()
                if command is not None:
                    await self._complete(command)
