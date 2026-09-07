"""Legacy deadlines retain transport extension calls and retry diagnostics."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
import stompman
from stompman.errors import AllServersUnavailable

from test_stompman.conftest import ScriptedBroker

pytestmark = pytest.mark.anyio


async def test_zero_connect_deadline_is_passed_to_the_custom_transport(broker: ScriptedBroker) -> None:
    connect = broker.connection_class.connect
    with patch.object(broker.connection_class, "connect", AsyncMock(wraps=connect)) as calls:
        async with broker.client(connect_timeout=0) as client:
            assert client.is_alive()
        assert calls.await_count == 1
        assert calls.call_args.kwargs["timeout"] == 0


async def test_expired_transport_attempts_keep_original_deadline_diagnostics(broker: ScriptedBroker) -> None:
    with (
        patch.object(broker.connection_class, "connect", AsyncMock(return_value=None)) as calls,
        pytest.raises(stompman.FailedAllConnectAttemptsError) as failure,
    ):
        await broker.client(connect_timeout=0).__aenter__()
    assert calls.await_count == 3
    assert all(call.kwargs["timeout"] == 0 for call in calls.call_args_list)
    assert len(failure.value.issues) == 3
    assert all(isinstance(issue, AllServersUnavailable) and issue.timeout == 0 for issue in failure.value.issues)


async def test_expired_handshake_still_submits_connect_on_every_attempt(broker: ScriptedBroker) -> None:
    with pytest.raises(stompman.FailedAllConnectAttemptsError) as failure:
        await broker.client(connection_confirmation_timeout=0).__aenter__()
    assert len(broker.connections) == 3
    for connection in broker.connections:
        assert connection.closed
        assert [type(frame) for frame in connection.writes] == [stompman.ConnectFrame]
        assert connection.read_calls == 0
    assert all(
        isinstance(issue, stompman.ConnectionConfirmationTimeout) and issue.timeout == 0 and not issue.frames
        for issue in failure.value.issues
    )


async def test_expired_handshake_preserves_retry_spacing(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    delays: list[float] = []
    original_sleep = asyncio.sleep

    async def sleep(delay: float) -> None:
        if delay > 0:
            delays.append(delay)
        await original_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", sleep)
    with pytest.raises(stompman.FailedAllConnectAttemptsError):
        await broker.client(connection_confirmation_timeout=0, connect_retry_interval=2).__aenter__()
    assert broker.connect_calls == 3
    assert delays == [2, 4]
