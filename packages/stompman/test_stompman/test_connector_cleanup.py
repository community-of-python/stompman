import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable

import pytest
from stompman.core import ConnectionSettings, Heartbeat, Server
from stompman.core.connector import Connector
from stompman.core.frames import AnyClientFrame, AnyServerFrame, ConnectedFrame
from stompman.core.transport import Transport


class HandshakingTransport:
    last_received_at = 0.0

    def __init__(self, close: Callable[[], Awaitable[None]]) -> None:
        self._close = close

    async def write_frame(self, frame: AnyClientFrame) -> None:
        pass

    async def send_heartbeat(self) -> None:
        pass

    async def read_frames(self) -> AsyncGenerator[AnyServerFrame, None]:
        yield ConnectedFrame(headers={"version": "1.2", "heart-beat": "0,0"})

    async def close(self) -> None:
        await self._close()


@pytest.mark.anyio
@pytest.mark.parametrize("cancel", [False, True])
async def test_connector_finishes_every_loser_cleanup_before_raising(*, cancel: bool) -> None:
    slow_started = asyncio.Event()
    slow_released = asyncio.Event()
    slow_closed = asyncio.Event()
    winner_closed = asyncio.Event()

    async def close_winner() -> None:
        winner_closed.set()

    async def close_failed() -> None:
        msg = "loser cleanup failed"
        raise RuntimeError(msg)

    async def close_slow() -> None:
        slow_started.set()
        await slow_released.wait()
        slow_closed.set()

    peers = {
        "winner": HandshakingTransport(close_winner),
        "failed": HandshakingTransport(close_failed),
        "slow": HandshakingTransport(close_slow),
    }
    servers = tuple(Server(name, 1, "", "") for name in peers)

    async def factory(server: Server, settings: ConnectionSettings) -> Transport:
        return peers[server.host]

    connector = Connector(lambda: servers, ConnectionSettings(heartbeat=Heartbeat(0, 0)), factory)
    connecting = asyncio.create_task(connector.connect())
    try:
        await slow_started.wait()
        completed, _ = await asyncio.wait({connecting}, timeout=0.01)
        assert not completed
        if cancel:
            connecting.cancel()
            await asyncio.sleep(0)
            connecting.cancel()
            completed, _ = await asyncio.wait({connecting}, timeout=0.01)
            assert not completed
        assert not slow_closed.is_set()
        slow_released.set()
        if cancel:
            with pytest.raises(asyncio.CancelledError) as info:
                await connecting
            assert isinstance(info.value.__cause__, RuntimeError)
        else:
            with pytest.raises(RuntimeError, match="loser cleanup failed"):
                await connecting
        assert slow_closed.is_set()
        assert winner_closed.is_set()
    finally:
        slow_released.set()
        await asyncio.gather(connecting, return_exceptions=True)
        await asyncio.wait_for(slow_closed.wait(), timeout=1)
