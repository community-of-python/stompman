from dataclasses import dataclass, field
from types import TracebackType
from typing import Self
from uuid import uuid4

from stompman.connection import AbstractConnection
from stompman.connection_manager import ConnectionManager
from stompman.frames import AbortFrame, BeginFrame, CommitFrame, SendFrame

ActiveTransactions = set["Transaction"]


@dataclass(kw_only=True, slots=True, unsafe_hash=True)
class Transaction:
    id: str = field(default_factory=lambda: _make_transaction_id(), init=False)  # noqa: PLW0108
    _connection_manager: ConnectionManager = field(hash=False)
    _active_transactions: ActiveTransactions = field(hash=False)
    sent_frames: list[SendFrame] = field(default_factory=list, init=False, hash=False)

    async def __aenter__(self) -> Self:
        await self._connection_manager.write_frame_reconnecting(BeginFrame(headers={"transaction": self.id}))
        self._active_transactions.add(self)
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        if exc_value:
            await self._connection_manager.maybe_write_frame(AbortFrame(headers={"transaction": self.id}))
            self._active_transactions.remove(self)
        else:
            committed = await self._connection_manager.maybe_write_frame(CommitFrame(headers={"transaction": self.id}))
            if committed:
                self._active_transactions.remove(self)

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
        self.sent_frames.append(frame)
        await self._connection_manager.write_frame_reconnecting(frame)


def _make_transaction_id() -> str:
    return str(uuid4())


async def commit_pending_transactions(
    *, active_transactions: ActiveTransactions, connection: AbstractConnection
) -> None:
    for transaction in active_transactions:
        for frame in transaction.sent_frames:
            await connection.write_frame(frame)
        await connection.write_frame(CommitFrame(headers={"transaction": transaction.id}))
    active_transactions.clear()
