import asyncio
from collections.abc import AsyncGenerator, Coroutine
from typing import Any
from unittest import mock

import faker
import pytest
import stompman.connection_lifespan
from stompman import (
    AnyServerFrame,
    Client,
    ConnectedFrame,
    ConnectFrame,
    ConnectionConfirmationTimeout,
    ConnectionParameters,
    DisconnectFrame,
    ErrorFrame,
    FailedAllConnectAttemptsError,
    ReceiptFrame,
    UnsupportedProtocolVersion,
)

from test_stompman.conftest import (
    BaseMockConnection,
    EnrichedClient,
    build_dataclass,
    create_spying_connection,
    get_read_frames_with_lifespan,
)

pytestmark = pytest.mark.anyio


async def test_client_connection_lifespan_ok(monkeypatch: pytest.MonkeyPatch, faker: faker.Faker) -> None:
    connected_frame = build_dataclass(ConnectedFrame, headers={"version": Client.PROTOCOL_VERSION, "heart-beat": "1,1"})
    connection_class, collected_frames = create_spying_connection(
        [connected_frame], [], [(receipt_frame := build_dataclass(ReceiptFrame))]
    )

    disconnect_frame = DisconnectFrame(headers={"receipt": (receipt_id := faker.pystr())})
    monkeypatch.setattr(stompman.connection_lifespan, "_make_receipt_id", mock.Mock(return_value=receipt_id))

    async with EnrichedClient(
        [ConnectionParameters("localhost", 10, "login", "%3Dpasscode")], connection_class=connection_class
    ) as client:
        await asyncio.sleep(0)

    connect_frame = ConnectFrame(
        headers={
            "host": "localhost",
            "accept-version": Client.PROTOCOL_VERSION,
            "heart-beat": client.heartbeat.to_header(),
            "login": "login",
            "passcode": "=passcode",
        }
    )
    assert collected_frames == [connect_frame, connected_frame, disconnect_frame, receipt_frame]


@pytest.mark.usefixtures("mock_sleep")
async def test_client_connection_lifespan_connection_not_confirmed(
    monkeypatch: pytest.MonkeyPatch, faker: faker.Faker
) -> None:
    async def mock_wait_for(future: Coroutine[Any, Any, Any], timeout: float) -> object:
        assert timeout == connection_confirmation_timeout
        task = asyncio.create_task(future)
        await asyncio.sleep(0)
        return await original_wait_for(task, 0)

    original_wait_for = asyncio.wait_for
    monkeypatch.setattr("asyncio.wait_for", mock_wait_for)
    error_frame = build_dataclass(ErrorFrame)
    connection_confirmation_timeout = faker.pyint()

    class MockConnection(BaseMockConnection):
        @staticmethod
        async def read_frames() -> AsyncGenerator[AnyServerFrame, None]:
            yield error_frame
            await asyncio.sleep(0)

    with pytest.raises(FailedAllConnectAttemptsError) as exc_info:
        await EnrichedClient(
            connection_class=MockConnection, connection_confirmation_timeout=connection_confirmation_timeout
        ).__aenter__()

    assert exc_info.value == FailedAllConnectAttemptsError(
        retry_attempts=3,
        issues=[ConnectionConfirmationTimeout(timeout=connection_confirmation_timeout, frames=[error_frame])] * 3,
    )


@pytest.mark.usefixtures("mock_sleep")
async def test_client_connection_lifespan_unsupported_protocol_version(faker: faker.Faker) -> None:
    given_version = faker.pystr()

    with pytest.raises(FailedAllConnectAttemptsError) as exc_info:
        await EnrichedClient(
            connection_class=create_spying_connection(
                [build_dataclass(ConnectedFrame, headers={"version": given_version})]
            )[0],
            connect_retry_attempts=1,
        ).__aenter__()

    assert exc_info.value == FailedAllConnectAttemptsError(
        retry_attempts=1,
        issues=[UnsupportedProtocolVersion(given_version=given_version, supported_version=Client.PROTOCOL_VERSION)],
    )


async def test_client_connection_lifespan_disconnect_not_confirmed(
    monkeypatch: pytest.MonkeyPatch, faker: faker.Faker
) -> None:
    wait_for_calls = []

    async def mock_wait_for(future: Coroutine[Any, Any, Any], timeout: float) -> object:
        wait_for_calls.append(timeout)
        task = asyncio.create_task(future)
        await asyncio.sleep(0)
        return await original_wait_for(task, 0)

    original_wait_for = asyncio.wait_for
    monkeypatch.setattr("asyncio.wait_for", mock_wait_for)
    disconnect_confirmation_timeout = faker.pyint()
    read_frames_yields = get_read_frames_with_lifespan([])
    read_frames_yields[-1].clear()
    connection_class, _ = create_spying_connection(*read_frames_yields)

    async with EnrichedClient(
        connection_class=connection_class, disconnect_confirmation_timeout=disconnect_confirmation_timeout
    ):
        pass

    assert wait_for_calls[-1] == disconnect_confirmation_timeout


async def test_client_heartbeats_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    async def mock_sleep(delay: float) -> None:
        await real_sleep(0)
        sleep_calls.append(delay)

    sleep_calls: list[float] = []
    real_sleep = asyncio.sleep
    monkeypatch.setattr("asyncio.sleep", mock_sleep)

    connection_class, _ = create_spying_connection(*get_read_frames_with_lifespan([]))
    connection_class.write_heartbeat = (write_heartbeat_mock := mock.Mock())  # type: ignore[method-assign]

    async with EnrichedClient(connection_class=connection_class):
        await real_sleep(0)
        await real_sleep(0)
        await real_sleep(0)

    assert sleep_calls == [0, 0, 1, 3, 1, 3]
    assert write_heartbeat_mock.mock_calls == [mock.call(), mock.call(), mock.call()]


def test_make_receipt_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    stompman.connection_lifespan._make_receipt_id()
