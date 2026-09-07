import asyncio

import pytest
import stompman
from stompman.core import Runtime
from stompman.core.errors import FailedAllConnectAttemptsError, HandshakeRejected

from test_stompman.conftest import ScriptedBroker, wait_until

pytestmark = pytest.mark.anyio


async def test_handshake_and_disconnect_use_one_reader(broker: ScriptedBroker) -> None:
    servers = [
        stompman.ConnectionParameters("localhost", 10, "login", "%3Dpasscode", connect_headers={"client-id": "one"})
    ]
    async with broker.client(servers=servers) as client:
        assert client.is_alive()
    connection = broker.current
    connect = connection.writes[0]
    assert isinstance(connect, stompman.ConnectFrame)
    assert connect.headers == {
        "accept-version": "1.2",
        "host": "localhost",
        "heart-beat": "0,0",
        "login": "login",
        "passcode": "=passcode",
        "client-id": "one",
    }
    assert isinstance(connection.writes[-1], stompman.DisconnectFrame)
    assert connection.read_calls == 1
    assert connection.closed


@pytest.mark.parametrize("response", [None, stompman.ErrorFrame(headers={"message": "denied"})])
async def test_handshake_failure_closes_every_candidate(
    broker: ScriptedBroker, response: stompman.AnyServerFrame | None
) -> None:
    broker.handshakes["localhost"] = response
    with pytest.raises(FailedAllConnectAttemptsError) as info:
        await broker.runtime(connection_confirmation_timeout=0.001).start()
    assert info.value.retry_attempts == 3
    assert len(info.value.issues) == 3
    expected = stompman.ConnectionConfirmationTimeout if response is None else HandshakeRejected
    assert all(isinstance(issue, expected) for issue in info.value.issues)
    assert all(connection.closed for connection in broker.connections)


async def test_unsupported_version(broker: ScriptedBroker) -> None:
    broker.handshakes["localhost"] = stompman.ConnectedFrame(headers={"version": "1.0"})
    with pytest.raises(FailedAllConnectAttemptsError) as info:
        await broker.runtime(connect_retry_attempts=1).start()
    assert info.value.issues == [stompman.UnsupportedProtocolVersion(given_version="1.0", supported_version="1.2")]
    assert broker.current.closed


async def test_failed_start_can_be_retried(broker: ScriptedBroker) -> None:
    broker.available = False
    runtime = broker.runtime()
    with pytest.raises(FailedAllConnectAttemptsError):
        await runtime.start()
    assert runtime.status.state == "closed"
    broker.available = True
    async with runtime:
        assert runtime.is_alive()


async def test_cancelled_start_closes_handshake_candidates(broker: ScriptedBroker) -> None:
    broker.handshakes["localhost"] = None
    runtime = broker.runtime(connection_confirmation_timeout=60)
    task = asyncio.create_task(runtime.start())
    await wait_until(lambda: bool(broker.connections))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.current.closed
    assert runtime.status.state == "closed"


async def test_disconnect_receipt_timeout_is_bounded(broker: ScriptedBroker) -> None:
    broker.receipts = False
    async with broker.runtime():
        pass
    assert broker.current.closed


async def test_close_and_restart_are_idempotent(broker: ScriptedBroker) -> None:
    runtime = broker.runtime()
    await runtime.close()
    async with runtime:
        await runtime.start()
        assert broker.connect_calls == 1
    await runtime.close()
    async with runtime:
        assert runtime.is_alive()
        assert broker.connect_calls == 2


async def test_context_body_error_is_preserved(broker: ScriptedBroker) -> None:
    with pytest.raises(ValueError, match="body"):
        async with broker.client():
            msg = "body"
            raise ValueError(msg)
    assert broker.current.closed


async def test_legacy_client_is_dataclass_extendable(broker: ScriptedBroker) -> None:
    client = broker.client()
    assert isinstance(client.core, Runtime)
    async with client:
        assert broker.connect_calls == 1
        assert client.is_alive()
