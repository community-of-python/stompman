import ast
import asyncio
from pathlib import Path

import pytest
import stompman
import stompman.core
from stompman.core import Delivery

from test_stompman.conftest import ScriptedBroker, ScriptedConnection, wait_until

pytestmark = pytest.mark.anyio


async def test_graceful_drain_allows_publish_and_ack_before_unsubscribe(broker: ScriptedBroker) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    runtime = broker.runtime()

    async def handle(delivery: Delivery) -> None:
        entered.set()
        await release.wait()
        await runtime.send(b"response", "reply")
        await delivery.ack()

    await runtime.start()
    sub = await runtime.subscribe("q", handle)
    broker.current.deliver(sub.id, b"one", ack_id="a")
    await entered.wait()
    close_task = asyncio.create_task(runtime.close())
    await wait_until(lambda: not runtime.is_alive())
    release.set()
    await close_task
    commands = [type(frame) for frame in broker.current.writes]
    assert commands.index(stompman.AckFrame) < commands.index(stompman.UnsubscribeFrame)
    assert commands[-1] is stompman.DisconnectFrame
    assert runtime.status.pending_messages == 0


async def test_cancelled_race_cleanup_closes_winner_too(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    original_close = broker.connection_class.close

    async def close(connection: ScriptedConnection) -> None:
        if connection.host == "second":
            entered.set()
            await release.wait()
        await original_close(connection)

    monkeypatch.setattr(broker.connection_class, "close", close)
    servers = [stompman.ConnectionParameters(host, 10, "login", "pass") for host in ("first", "second")]
    runtime = broker.runtime(servers=servers)
    task = asyncio.create_task(runtime.start())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runtime.status.state == "closed"
    assert all(connection.closed for connection in broker.connections)


async def test_receipts_match_exact_id(broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch) -> None:
    original_write = broker.connection_class.write_frame

    async def write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
        connection.incoming.put_nowait(stompman.ReceiptFrame(headers={"receipt-id": "unrelated"}))
        await original_write(connection, frame)

    monkeypatch.setattr(broker.connection_class, "write_frame", write)
    async with broker.runtime() as runtime:
        receipt = await runtime.send(b"one", "q", receipt_timeout=0.1)
        assert receipt is not None
        assert receipt.headers["receipt-id"] != "unrelated"
        sent_frame = broker.current.writes[-1]
        assert isinstance(sent_frame, stompman.SendFrame)
        assert receipt.headers["receipt-id"] == sent_frame.headers["receipt"]
        await runtime.send(b"unconfirmed", "q")


async def test_receipt_timeout_does_not_replay(broker: ScriptedBroker) -> None:
    broker.receipts = False
    async with broker.runtime() as runtime:
        with pytest.raises(stompman.ReceiptTimeoutError):
            await runtime.send(b"one", "q", receipt_timeout=0.001)
        assert sum(isinstance(frame, stompman.SendFrame) for frame in broker.current.writes) == 1
        assert runtime.is_alive()


async def test_connection_loss_waiting_for_receipt_does_not_replay(broker: ScriptedBroker) -> None:
    async with broker.runtime() as runtime:
        first = broker.current
        broker.fail_after = lambda frame, connection: connection is first and isinstance(frame, stompman.SendFrame)
        with pytest.raises(stompman.ConnectionLostError):
            await runtime.send(b"one", "q", receipt_timeout=1)
        await runtime.send(b"after", "q")
        assert first.closed
        assert [frame.body for frame in broker.current.writes if isinstance(frame, stompman.SendFrame)] == [b"after"]


async def test_callback_error_is_fatal_and_preserved(broker: ScriptedBroker) -> None:
    def on_error(frame: stompman.ErrorFrame) -> None:
        msg = "callback failed"
        raise ValueError(msg)

    with pytest.raises(ExceptionGroup) as info:
        async with broker.runtime(on_error_frame=on_error):
            broker.current.incoming.put_nowait(stompman.ErrorFrame(headers={"message": "error"}))
            await asyncio.Future()
    assert any(isinstance(error, ValueError) and str(error) == "callback failed" for error in info.value.exceptions)
    assert broker.current.closed


@pytest.mark.parametrize(
    "option",
    [
        "max_concurrent_handlers",
        "max_pending_messages",
        "max_pending_bytes",
        "write_retry_attempts",
        "connect_retry_attempts",
    ],
)
async def test_invalid_capacity_and_retry_configuration(broker: ScriptedBroker, option: str) -> None:
    with pytest.raises(ValueError, match="must be positive"):
        await broker.runtime(**{option: 0}).start()


async def test_client_configuration_snapshot_is_independent(broker: ScriptedBroker) -> None:
    client = broker.client()
    config = client.to_config()
    client.servers.append(stompman.ConnectionParameters("other", 1, "u", "p"))
    client.servers[0].connect_headers["client-id"] = "later"
    assert len(config.servers) == 1
    assert config.servers[0].connect_headers == {}


def test_core_dependency_direction() -> None:
    forbidden = {
        "stompman.client",
        "stompman.subscription",
        "stompman.transaction",
        "stompman.connection_manager",
        "stompman.connection_lifespan",
    }
    for source in Path(stompman.core.__file__).parent.glob("*.py"):
        tree = ast.parse(source.read_text())
        imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
        assert not imports & forbidden, source.name


async def test_malformed_server_heartbeat_fails_without_a_busy_loop(broker: ScriptedBroker) -> None:
    broker.heartbeat = "-1,1"
    with pytest.raises(stompman.FailedAllConnectAttemptsError):
        await broker.runtime(connect_retry_attempts=1).start()
    assert broker.current.closed
