from dataclasses import dataclass
from types import TracebackType
from typing import Self
from uuid import uuid4

from stompman.core.transaction import Transaction as CoreTransaction
from stompman.frames import SendFrame


@dataclass(slots=True)
class Transaction:
    _transaction: CoreTransaction

    @property
    def id(self) -> str:
        return self._transaction.id

    @property
    def sent_frames(self) -> list[SendFrame]:
        return self._transaction.sent_frames

    async def __aenter__(self) -> Self:
        await self._transaction.__aenter__()
        return self

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        await self._transaction.__aexit__(exc_type, exc_value, traceback)

    async def send(
        self,
        body: bytes,
        destination: str,
        *,
        content_type: str | None = None,
        add_content_length: bool = True,
        headers: dict[str, str] | None = None,
    ) -> None:
        await self._transaction.send(
            body, destination, content_type=content_type, add_content_length=add_content_length, headers=headers
        )


def _make_transaction_id() -> str:
    return str(uuid4())
