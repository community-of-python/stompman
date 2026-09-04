import asyncio
from collections import deque
from datetime import timedelta

import pytest
import stompman

from test_stompman.conftest import ScriptedBroker, wait_until

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("attempt", [1, 2, 3])
async def test_connect_retry_count(broker: ScriptedBroker, attempt: int) -> None:
    broker.connect_results = deque([False] * (attempt - 1) + [True])
    async with broker.runtime():
        assert broker.connect_calls == attempt


async def test_unavailable_servers_exhaust_retries(broker: ScriptedBroker) -> None:
    broker.available = False
    with pytest.raises(stompman.FailedAllConnectAttemptsError):
        await broker.runtime().start()
    assert broker.connect_calls == 3


async def test_handshake_success_wins_race(broker: ScriptedBroker) -> None:
    broker.handshakes["bad"] = stompman.ConnectedFrame(headers={"version": "1.0"})
    broker.delays["healthy"] = 0.01
    broker.delays["pending"] = 60
    servers = [stompman.ConnectionParameters(host, 10, "login", "pass") for host in ("bad", "healthy", "pending")]
    async with broker.runtime(servers=servers) as runtime:
        assert runtime.is_alive()
        assert next(c for c in broker.connections if c.host == "bad").closed
        assert "pending" in broker.cancelled_hosts
        await runtime.send(b"hello", "queue")
        assert isinstance(next(c for c in broker.connections if c.host == "healthy").writes[-1], stompman.SendFrame)
    assert all(connection.closed for connection in broker.connections)


async def test_simultaneous_handshake_losers_are_closed(broker: ScriptedBroker) -> None:
    servers = [stompman.ConnectionParameters(host, 10, "login", "pass") for host in ("first", "second", "third")]
    async with broker.runtime(servers=servers):
        assert len([c for c in broker.connections if not c.closed]) == 1
    assert all(c.closed for c in broker.connections)


async def test_concurrent_writes_share_recovery(broker: ScriptedBroker) -> None:
    async with broker.runtime() as runtime:
        broker.current.incoming.put_nowait(stompman.ConnectionLostError(reason="lost"))
        await asyncio.gather(*(runtime.send(str(index).encode(), "q") for index in range(20)))
        await wait_until(lambda: runtime.status.generation == 2)
        assert broker.connect_calls == 2
        assert broker.connections[0].closed
        assert sum(isinstance(frame, stompman.SendFrame) for c in broker.connections for frame in c.writes) == 20


async def test_write_failure_retries_on_new_session(broker: ScriptedBroker) -> None:
    async with broker.runtime() as runtime:
        first = broker.current
        broker.fail_before = lambda frame, connection: connection is first and isinstance(frame, stompman.SendFrame)
        await runtime.send(b"one", "q")
        assert runtime.status.generation == 2
        assert first.closed
        assert len([f for f in broker.current.writes if isinstance(f, stompman.SendFrame)]) == 1


async def test_write_attempts_exhaustion(broker: ScriptedBroker) -> None:
    async with broker.runtime() as runtime:
        broker.fail_before = lambda frame, connection: isinstance(frame, stompman.SendFrame)
        with pytest.raises(stompman.FailedAllWriteAttemptsError) as info:
            await runtime.send(b"one", "q")
        assert info.value.retry_attempts == 3
        assert broker.connect_calls == 3
        broker.fail_before = None
        await runtime.send(b"two", "q")


async def test_background_recovery_is_fatal_by_default(broker: ScriptedBroker) -> None:
    with pytest.raises(ExceptionGroup) as info:
        async with broker.runtime():
            broker.available = False
            broker.current.incoming.put_nowait(stompman.ConnectionLostError(reason="lost"))
            await asyncio.Future()
    assert any(isinstance(error, stompman.FailedAllConnectAttemptsError) for error in info.value.exceptions)
    assert broker.current.closed


async def test_keep_alive_recovers_after_exhausted_cycles(broker: ScriptedBroker) -> None:
    async with broker.runtime(keep_alive_on_connection_failure=True, connect_retry_interval=0.001) as runtime:
        broker.available = False
        broker.current.incoming.put_nowait(stompman.ConnectionLostError(reason="lost"))
        await wait_until(lambda: broker.connect_calls >= 5)
        assert not runtime.is_alive()
        broker.available = True
        await wait_until(runtime.is_alive)
        assert runtime.status.generation == 2
        await runtime.send(b"after recovery", "q")


async def test_idle_restart_and_disable(broker: ScriptedBroker) -> None:
    async with broker.runtime(no_message_restart_interval=timedelta(milliseconds=10)) as runtime:
        await wait_until(lambda: runtime.status.generation >= 2)
    async with broker.runtime(no_message_restart_interval=None) as runtime:
        generation = runtime.status.generation
        await asyncio.sleep(0.02)
        assert runtime.status.generation == generation


async def test_pending_error_after_connected_is_delivered(broker: ScriptedBroker) -> None:
    errors: list[stompman.ErrorFrame] = []
    error = stompman.ErrorFrame(headers={"message": "server rejected request"})
    broker.after_connected = [error]

    def on_error(frame: stompman.ErrorFrame) -> None:
        errors.append(frame)
        broker.after_connected.clear()

    async with broker.runtime(on_error_frame=on_error) as runtime:
        await wait_until(lambda: runtime.status.generation == 2)
        assert errors == [error]
