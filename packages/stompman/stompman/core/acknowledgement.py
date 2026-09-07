"""A wire acknowledgement is a capability bound to its original session."""

from dataclasses import dataclass
from enum import Enum, auto
from typing import TYPE_CHECKING, Protocol

from .config import Confirmation
from .frames import AckFrame, NackFrame

if TYPE_CHECKING:
    from .session import Session


class Decision(Enum):
    ACCEPT = auto()
    REJECT = auto()


class Acknowledgement(Protocol):
    async def send(self, decision: Decision) -> None: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class MessageAcknowledgement:
    id: str
    subscription_id: str
    session: "Session"
    confirmation: Confirmation

    async def send(self, decision: Decision) -> None:
        frame_type = AckFrame if decision is Decision.ACCEPT else NackFrame
        await self.session.write(
            frame_type(headers={"id": self.id, "subscription": self.subscription_id}), self.confirmation
        )
