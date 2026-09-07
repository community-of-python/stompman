"""Session-bound acknowledgement capabilities and their wire ordering.

Queue membership means unsettled; removal means terminal. Only this owner can
choose a decision, send it, or release its capacity. Cumulative acknowledgement
flushes a decided prefix, so a later ACK cannot acknowledge an earlier handler.
"""

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from enum import Enum, auto

from .acknowledgement import Acknowledgement, Decision
from .capacity import Completion
from .config import Confirmation
from .errors import ConnectionLostError
from .frames import MessageFrame
from .session import Session


class Pending(Enum):
    DECISION = auto()


@dataclass(frozen=True, slots=True)
class AutomaticSettlement:
    """The broker has already settled an auto-acknowledged message."""

    async def settle(self, *, accepted: bool) -> None:
        pass

    def retire(self) -> None:
        pass


@dataclass(frozen=True, slots=True, eq=False)
class ManualSettlement:
    owner: "ManualAcknowledgements"
    acknowledgement: Acknowledgement
    completed: Completion

    async def settle(self, *, accepted: bool) -> None:
        await self.owner.settle(self, Decision.ACCEPT if accepted else Decision.REJECT)

    def retire(self) -> None:
        self.owner.retire(self)


Settlement = AutomaticSettlement | ManualSettlement


class AutomaticAcknowledgements:
    @staticmethod
    def register(_frame: MessageFrame, completed: Completion) -> AutomaticSettlement:
        completed.finish()
        return AutomaticSettlement()

    def close(self) -> None:
        pass

    async def finish(self) -> None:
        pass


class ManualAcknowledgements:
    def __init__(self, session: Session, subscription_id: str, confirmation: Confirmation, *, cumulative: bool) -> None:
        self._session = session
        self._subscription_id = subscription_id
        self._confirmation = confirmation
        self._cumulative = cumulative
        self._pending: OrderedDict[ManualSettlement, Pending | Decision] = OrderedDict()
        self._writing = asyncio.Lock()

    def register(self, frame: MessageFrame, completed: Completion) -> ManualSettlement:
        acknowledgement = self._session.acknowledgement(frame, self._subscription_id, self._confirmation)
        settlement = ManualSettlement(self, acknowledgement, completed)
        self._pending[settlement] = Pending.DECISION
        return settlement

    def retire(self, settlement: ManualSettlement) -> None:
        self._pending.pop(settlement, None)
        settlement.completed.finish()

    def close(self) -> None:
        for settlement in self._pending:
            settlement.completed.finish()
        self._pending.clear()

    async def finish(self) -> None:
        """Let every already requested settlement finish before retiring."""
        async with self._writing:
            self.close()

    async def settle(self, settlement: ManualSettlement, decision: Decision) -> None:
        async with self._writing:
            if self._pending.get(settlement) is not Pending.DECISION:
                return
            if self._cumulative:
                self._pending[settlement] = decision
                await self._flush_prefix()
            else:
                del self._pending[settlement]
                await self._send(settlement, decision)

    async def _flush_prefix(self) -> None:
        while self._pending:
            settlement, decision = next(iter(self._pending.items()))
            if isinstance(decision, Pending):
                break
            del self._pending[settlement]
            try:
                await self._send(settlement, decision)
            except BaseException:
                # Later decisions already returned to their callers. Retiring the
                # session lets the broker redeliver them without replaying an
                # acknowledgement whose outcome is unknown.
                self._session.fail(ConnectionLostError(reason="cumulative settlement was interrupted"))
                raise

    async def _send(self, settlement: ManualSettlement, decision: Decision) -> None:
        try:
            if self._session.ended.done():
                return
            await settlement.acknowledgement.send(decision)
        except ConnectionLostError:
            pass
        finally:
            settlement.completed.finish()
