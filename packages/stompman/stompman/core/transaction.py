import asyncio
import contextlib
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType
from typing import TYPE_CHECKING, Self, cast
from uuid import uuid4

from stompman.errors import ConnectionLostError, TransactionOutcomeUnknownError
from stompman.frames import AbortFrame, BeginFrame, CommitFrame, SendFrame, SendHeaders

if TYPE_CHECKING:
    from stompman.core.runtime import Runtime
    from stompman.core.session import Session


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
        self, runtime: "Runtime", *, receipt_timeout: float | None = None, transaction_id: str | None = None
    ) -> None:
        self.id = transaction_id or str(uuid4())
        self.state = TransactionState.NEW
        self._runtime = runtime
        self._receipt_timeout = receipt_timeout
        self._journal: list[JournalEntry] = []
        self._generation = 0

    @property
    def sent_frames(self) -> list[SendFrame]:
        """Independent snapshots; callers cannot mutate the replay journal."""
        return [entry.frame() for entry in self._journal]

    def _require_open(self) -> None:
        if self.state is not TransactionState.OPEN:
            msg = f"transaction is {self.state.value}"
            raise RuntimeError(msg)
        self._runtime._require_open()

    async def __aenter__(self) -> Self:
        async with self._runtime._lock:
            self._runtime._require_open()
            if self.state is not TransactionState.NEW or self.id in self._runtime._transactions:
                msg = "transaction has already been started"
                raise RuntimeError(msg)
            await self._runtime._write(BeginFrame(headers={"transaction": self.id}))
            assert self._runtime._session is not None  # ruff: ignore[assert]
            self._generation = self._runtime._session.generation
            self._runtime._transactions[self.id] = self
            self.state = TransactionState.OPEN
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        if exc_type is not None:
            await self.abort()
        else:
            await self.commit()

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
        async with self._runtime._lock:
            self._require_open()
            await self._runtime._write(frame)
            self._journal.append(JournalEntry.from_frame(frame))

    async def restore(self, session: "Session") -> None:
        if self.state not in {TransactionState.OPEN, TransactionState.COMMIT_REQUESTED}:
            return
        await session.write(BeginFrame(headers={"transaction": self.id}))
        for entry in self._journal:
            await session.write(entry.frame())
        self._generation = session.generation

    async def commit(self) -> None:
        async with self._runtime._lock:
            self._require_open()
            self.state = TransactionState.COMMIT_REQUESTED
            try:
                session = await self._runtime._ensure_session()
            except BaseException:
                self.state = TransactionState.ABORTED
                self._runtime._transactions.pop(self.id, None)
                raise
            try:
                await session.write(
                    CommitFrame(headers={"transaction": self.id}), receipt_timeout=self._receipt_timeout
                )
            except asyncio.CancelledError:
                self.state = TransactionState.UNCERTAIN
                session.fail(ConnectionLostError(reason="commit was cancelled"))
                raise
            except Exception as error:
                self.state = TransactionState.UNCERTAIN
                raise TransactionOutcomeUnknownError(transaction_id=self.id, reason=error) from error
            else:
                self.state = TransactionState.COMPLETED
            finally:
                self._runtime._transactions.pop(self.id, None)

    async def abort(self) -> None:
        async with self._runtime._lock:
            if self.state is not TransactionState.OPEN:
                return
            try:
                session = self._runtime._session
                if session is not None and not session.failed.is_set() and session.generation == self._generation:
                    with contextlib.suppress(ConnectionLostError):
                        await session.write(AbortFrame(headers={"transaction": self.id}))
            finally:
                self.state = TransactionState.ABORTED
                self._runtime._transactions.pop(self.id, None)
