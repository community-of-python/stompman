"""Race handshakes and retain exactly one negotiated transport.

All losing connections are closed, including candidates which finish while
the winner is being selected or the caller is being cancelled.
"""

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from ._tasks import await_cleanup
from .config import ConnectionSettings, Server
from .errors import AllServersUnavailable, AnyConnectionIssue, ConnectionLostError, ConnectionLostOnLifespanEnter
from .handshake import HandshakeFailedError, NegotiatedConnection
from .transport import TransportFactory


@dataclass(frozen=True, slots=True)
class Unavailable:
    issues: tuple[AnyConnectionIssue, ...]


async def close_connections(connections: Iterable[NegotiatedConnection]) -> None:
    """Finish every close before propagating a cleanup failure."""
    outcomes = await asyncio.gather(*(connection.close() for connection in connections), return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException):
            raise outcome


class Connector:
    def __init__(
        self, servers: Callable[[], tuple[Server, ...]], settings: ConnectionSettings, factory: TransportFactory
    ) -> None:
        self._servers = servers
        self._settings = settings
        self._factory = factory

    async def _connect(self, server: Server) -> NegotiatedConnection | Unavailable:
        try:
            transport = await self._factory(server, self._settings)
        except (OSError, ConnectionLostError):
            return Unavailable((AllServersUnavailable(servers=[server], timeout=self._settings.timeout),))
        try:
            return await NegotiatedConnection.open(transport, server, self._settings)
        except HandshakeFailedError as error:
            return Unavailable((error.issue,))
        except (OSError, ConnectionLostError, ValueError):
            return Unavailable((ConnectionLostOnLifespanEnter(),))

    async def connect(self) -> NegotiatedConnection | Unavailable:
        servers = self._servers()
        if not servers:
            return Unavailable((AllServersUnavailable(servers=[], timeout=self._settings.timeout),))
        tasks = [asyncio.create_task(self._connect(server), name="stomp-connect") for server in servers]
        winner: NegotiatedConnection | None = None
        issues: list[AnyConnectionIssue] = []

        async def cleanup() -> None:
            for task in tasks:
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            await close_connections(
                item for item in results if isinstance(item, NegotiatedConnection) and item is not winner
            )

        try:
            for completed in asyncio.as_completed(tasks):
                result = await completed
                if isinstance(result, NegotiatedConnection):
                    winner = result
                    break
                issues.extend(result.issues)
        finally:
            try:
                await await_cleanup(asyncio.create_task(cleanup()))
            except BaseException:
                if winner is not None:
                    await await_cleanup(asyncio.create_task(winner.close()))
                raise
        return winner if winner is not None else Unavailable(tuple(issues))
