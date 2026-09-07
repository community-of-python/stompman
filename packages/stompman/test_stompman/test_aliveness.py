import asyncio

import pytest
import stompman

from test_stompman.conftest import ScriptedBroker, wait_until

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("client", "server", "negotiated"),
    [
        ("10,20", "30,40", "40,30"),
        ("0,20", "30,40", "0,30"),
        ("10,0", "30,40", "40,0"),
        ("10,20", "0,0", "0,0"),
    ],
)
async def test_heartbeat_negotiation(broker: ScriptedBroker, client: str, server: str, negotiated: str) -> None:
    broker.heartbeat = server
    async with broker.runtime(heartbeat=stompman.Heartbeat.from_header(client)) as runtime:
        assert runtime.status.heartbeat.to_header() == negotiated
        assert runtime.is_alive()


async def test_no_heartbeat_means_no_busy_sender_and_healthy(broker: ScriptedBroker) -> None:
    async with broker.runtime() as runtime:
        await asyncio.sleep(0.02)
        assert broker.current.heartbeats == 0
        assert runtime.is_alive()
    assert not runtime.is_alive()


async def test_heartbeat_sender_is_negotiated_and_recovers(broker: ScriptedBroker) -> None:
    broker.heartbeat = "0,5"
    async with broker.runtime(heartbeat=stompman.Heartbeat(10, 0)) as runtime:
        await wait_until(lambda: broker.current.heartbeats > 0)
        broker.heartbeat_failure = True
        await wait_until(lambda: runtime.status.generation >= 2)
        broker.heartbeat_failure = False
        assert broker.connections[0].closed


async def test_receive_heartbeat_timeout_reconnects_without_writes(broker: ScriptedBroker) -> None:
    broker.heartbeat = "5,0"
    async with broker.runtime(heartbeat=stompman.Heartbeat(0, 5)) as runtime:
        await wait_until(lambda: runtime.status.generation >= 2)
        assert broker.connections[0].closed
