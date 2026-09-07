"""Failure contracts for the legacy manager's public extension hooks."""

import asyncio
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING, cast

import pytest
import stompman
from stompman._legacy_errors import translate_connection_errors
from stompman.connection_lifespan import ConnectionLifespan
from stompman.connection_manager import ConnectionManager, ConnectionRestoration
from stompman.core import errors as native
from stompman.core.config import Server
from stompman.errors import AllServersUnavailable, ConnectionLostOnLifespanEnter
from stompman.subscription import ActiveSubscriptions

from test_stompman.conftest import ScriptedBroker, wait_until

if TYPE_CHECKING:
    from stompman.connection import AbstractConnection

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]


def manager_with_restoration(broker: ScriptedBroker, restore: ConnectionRestoration) -> ConnectionManager:
    template = broker.client(connect_retry_attempts=3)._connection_manager
    return replace(
        template,
        lifespan_factory=partial(
            ConnectionLifespan,
            protocol_version="1.2",
            client_heartbeat=stompman.Heartbeat(0, 0),
            connection_confirmation_timeout=1,
            disconnect_confirmation_timeout=1,
            active_subscriptions=ActiveSubscriptions(),
        ),
        restore_connection=lambda connection: restore,
    )


async def test_group_translation_preserves_nested_shape_and_server_identity() -> None:
    servers = [
        stompman.ConnectionParameters("first", 1234, "u", "p", ws_uri_path="/first"),
        stompman.ConnectionParameters("second", 1234, "u", "p", ws_uri_path="/second"),
    ]
    issue = native.FailedAllConnectAttemptsError(
        retry_attempts=2,
        issues=[
            native.AllServersUnavailable(servers=[Server(server.host, server.port, "u", "p")], timeout=0.125)
            for _attempt in range(2)
            for server in servers
        ],
    )
    unrelated = ValueError("unrelated")
    cancelled = asyncio.CancelledError("cancelled")
    inner = ExceptionGroup("inner", [issue, unrelated])
    inner.add_note("inner note")
    original = BaseExceptionGroup("outer", [inner, cancelled])
    original.add_note("outer note")
    with pytest.raises(BaseExceptionGroup) as failure, translate_connection_errors(servers):
        raise original
    translated = failure.value
    assert type(translated) is BaseExceptionGroup
    assert translated.message == "outer"
    assert translated.__notes__ == ["outer note"]
    nested = translated.exceptions[0]
    assert isinstance(nested, ExceptionGroup)
    assert nested.message == "inner"
    assert nested.__notes__ == ["inner note"]
    assert nested.exceptions[1] is unrelated
    assert translated.exceptions[1] is cancelled
    connection_error = nested.exceptions[0]
    assert isinstance(connection_error, stompman.FailedAllConnectAttemptsError)
    assert len(connection_error.issues) == 2
    for diagnostic in connection_error.issues:
        assert isinstance(diagnostic, AllServersUnavailable)
        assert diagnostic.servers is servers
        assert diagnostic.servers[0] is servers[0]
        assert diagnostic.timeout == pytest.approx(0.125)


@pytest.mark.parametrize("timeout", [0, 0.125])
async def test_timeout_override_and_nested_translation_are_idempotent(timeout: float) -> None:
    servers = [stompman.ConnectionParameters("first", 1234, "u", "p")]
    native_error = native.FailedAllConnectAttemptsError(
        retry_attempts=1,
        issues=[native.AllServersUnavailable(servers=[Server("first", 1234, "u", "p")], timeout=2)],
    )
    with (
        pytest.raises(stompman.FailedAllConnectAttemptsError) as failure,
        translate_connection_errors(servers, timeout=cast("int", timeout)),
        translate_connection_errors(servers, timeout=cast("int", timeout)),
    ):
        raise native_error
    assert len(failure.value.issues) == 1
    issue = failure.value.issues[0]
    assert isinstance(issue, AllServersUnavailable)
    assert issue.timeout == timeout
    assert issue.servers is servers


async def test_background_recovery_failure_is_translated_on_client_exit(broker: ScriptedBroker) -> None:
    servers = [stompman.ConnectionParameters("first", 1234, "u", "p", ws_uri_path="/ws")]
    with pytest.raises(ExceptionGroup) as failure:
        async with broker.client(servers=servers, connect_retry_attempts=1):
            broker.available = False
            broker.current.incoming.put_nowait(stompman.ConnectionLostError(reason="lost"))
            await asyncio.Event().wait()
    error = failure.value.exceptions[0]
    assert isinstance(error, stompman.FailedAllConnectAttemptsError)
    issue = error.issues[0]
    assert isinstance(issue, AllServersUnavailable)
    assert issue.servers is servers
    assert issue.servers[0] is servers[0]


@pytest.mark.parametrize("initial", [True, False])
async def test_restoration_connection_loss_retries_initially_and_in_background(
    broker: ScriptedBroker, initial: bool
) -> None:
    calls = 0
    healthy = asyncio.Event()
    lost: list[AbstractConnection] = []

    async def restore() -> None:
        nonlocal calls
        calls += 1
        if calls == (1 if initial else 2):
            raise stompman.ConnectionLostError(reason="restore failed")
        healthy.set()

    manager = manager_with_restoration(broker, restore)
    manager.on_connection_lost = lost.append
    async with manager:
        if initial:
            assert calls == 2
        else:
            assert calls == 1
            healthy.clear()
            broker.current.incoming.put_nowait(stompman.ConnectionLostError(reason="force reconnect"))
            await healthy.wait()
            assert calls == 3
        await manager.write_frame_reconnecting(
            stompman.SendFrame.build(
                body=b"after restore",
                destination="q",
                transaction=None,
                content_type=None,
                add_content_length=True,
                headers=None,
            )
        )
        assert all(connection.closed for connection in broker.connections[:-1])
        assert len(lost) == len(broker.connections) - 1
    assert all(connection.closed for connection in broker.connections)


async def test_cancelling_a_waiter_does_not_cancel_shared_restoration(broker: ScriptedBroker) -> None:
    restoring = asyncio.Event()
    release = asyncio.Event()
    cancelled = asyncio.Event()

    async def restore() -> None:
        restoring.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise

    manager = manager_with_restoration(broker, restore)
    # Open through the runtime so this test can cancel an independent readiness waiter.
    async with manager.runtime:
        await restoring.wait()
        waiting = asyncio.create_task(manager._get_restored_connection_state())
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not cancelled.is_set()
        release.set()
        await manager._get_restored_connection_state()
    assert not cancelled.is_set()


async def test_shutdown_owns_cancellation_of_blocked_restoration(broker: ScriptedBroker) -> None:
    restoring = asyncio.Event()
    cleanup_started = asyncio.Event()
    cleanup_release = asyncio.Event()
    calls = 0

    async def restore() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            return
        restoring.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleanup_started.set()
            await cleanup_release.wait()

    manager = manager_with_restoration(broker, restore)
    await manager.__aenter__()
    broker.current.incoming.put_nowait(stompman.ConnectionLostError(reason="force reconnect"))
    await restoring.wait()
    closing = asyncio.create_task(manager.__aexit__(None, None, None))
    try:
        await cleanup_started.wait()
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
    finally:
        cleanup_release.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await wait_until(lambda: all(connection.closed for connection in broker.connections))


@pytest.mark.parametrize("attempts", [1, 3])
async def test_failed_initial_restoration_exhausts_its_retry_budget_and_closes(
    broker: ScriptedBroker, attempts: int
) -> None:
    async def restore() -> None:
        raise stompman.ConnectionLostError(reason="restore always fails")

    manager = manager_with_restoration(broker, restore)
    manager.connect_retry_attempts = attempts
    with pytest.raises(stompman.FailedAllConnectAttemptsError) as failure:
        await manager.__aenter__()
    assert failure.value.retry_attempts == attempts
    assert len(failure.value.issues) == attempts
    assert all(isinstance(issue, ConnectionLostOnLifespanEnter) for issue in failure.value.issues)
    assert manager._active_connection_state is None
    assert all(connection.closed for connection in broker.connections)
