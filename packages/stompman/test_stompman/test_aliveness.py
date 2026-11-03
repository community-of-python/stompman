import asyncio
import time

import pytest
from stompman import Client, ConnectedFrame, Heartbeat, ReceiptFrame

from test_stompman.conftest import EnrichedClient, build_dataclass, create_spying_connection

pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("mock_sleep")]


@pytest.mark.parametrize("is_alive", [True, False])
async def test_connection_alive(is_alive: bool) -> None:  # noqa: FBT001
    connection_class, _ = create_spying_connection(
        [ConnectedFrame(headers={"version": Client.PROTOCOL_VERSION, "heart-beat": "1000,1000"})],
        [],
        [build_dataclass(ReceiptFrame)],
    )
    client = await EnrichedClient(connection_class=connection_class).__aenter__()
    assert client._connection_manager._active_connection_state
    client._connection_manager._active_connection_state.connection.last_read_time = time.time() if is_alive else 10
    assert client.is_alive() == is_alive


async def test_is_alive_false_after_grace_when_last_read_time_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_class, _ = create_spying_connection(
        [ConnectedFrame(headers={"version": Client.PROTOCOL_VERSION, "heart-beat": "1000,1000"})],
        [],
        [build_dataclass(ReceiptFrame)],
    )
    client = await EnrichedClient(connection_class=connection_class).__aenter__()
    state = client._connection_manager._active_connection_state
    assert state is not None
    state.connection.last_read_time = None  # simulate a connection that has not yet read

    # within grace window → True
    monkeypatch.setattr(time, "time", lambda: state.connected_at + 1.0)
    assert client.is_alive() is True

    # past grace window (heartbeat 1000ms * factor 3 = 3s) → False
    monkeypatch.setattr(time, "time", lambda: state.connected_at + 10.0)
    assert client.is_alive() is False


async def test_is_alive_false_when_listen_task_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection_class, _ = create_spying_connection(
        [ConnectedFrame(headers={"version": Client.PROTOCOL_VERSION, "heart-beat": "1000,1000"})],
        [],
        [build_dataclass(ReceiptFrame)],
    )
    client = await EnrichedClient(connection_class=connection_class).__aenter__()
    state = client._connection_manager._active_connection_state
    assert state is not None
    state.connection.last_read_time = time.time()  # connection is fine

    # simulate listen task finishing with an unhandled exception
    fake_task: asyncio.Task[None] = asyncio.create_task(asyncio.sleep(0))
    await fake_task
    monkeypatch.setattr(client, "_listen_task", fake_task)  # done, no exception
    assert client.is_alive() is False, "listener done means not alive"


async def test_is_alive_waits_on_the_agreed_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    # STOMP 1.2: the server sends every MAX(sx, cy) ms. With sx=1000 and cy=5000, 10 s of silence is within
    # three heart-beat intervals, and 16 s is not.
    connection_class, _ = create_spying_connection(
        [ConnectedFrame(headers={"version": Client.PROTOCOL_VERSION, "heart-beat": "1000,1000"})],
        [],
        [build_dataclass(ReceiptFrame)],
    )
    client = await EnrichedClient(connection_class=connection_class, heartbeat=Heartbeat(1000, 5000)).__aenter__()
    state = client._connection_manager._active_connection_state
    assert state is not None
    state.connection.last_read_time = state.connected_at

    monkeypatch.setattr(time, "time", lambda: state.connected_at + 10.0)
    assert client.is_alive() is True
    monkeypatch.setattr(time, "time", lambda: state.connected_at + 16.0)
    assert client.is_alive() is False


async def test_is_alive_when_no_server_heartbeats_are_agreed(monkeypatch: pytest.MonkeyPatch) -> None:
    connection_class, _ = create_spying_connection(
        [ConnectedFrame(headers={"version": Client.PROTOCOL_VERSION, "heart-beat": "0,0"})],
        [],
        [build_dataclass(ReceiptFrame)],
    )
    client = await EnrichedClient(connection_class=connection_class).__aenter__()
    state = client._connection_manager._active_connection_state
    assert state is not None
    state.connection.last_read_time = state.connected_at

    monkeypatch.setattr(time, "time", lambda: state.connected_at + 3600.0)
    assert client.is_alive() is True


def test_server_heartbeat_check_needs_a_window_of_at_least_the_interval() -> None:
    # STOMP 1.2: the receiver may consider the connection dead only "inside a time window of at least <n> milliseconds".
    with pytest.raises(ValueError, match="check_server_alive_interval_factor"):
        EnrichedClient(check_server_heartbeat=True, check_server_alive_interval_factor=0)
