import asyncio
from collections.abc import AsyncGenerator, Coroutine
from ssl import SSLContext
from typing import TYPE_CHECKING, Any, Literal, Self, cast
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
    ConnectionLostError,
    ConnectionParameters,
    DisconnectFrame,
    ErrorFrame,
    FailedAllConnectAttemptsError,
    Heartbeat,
    ReceiptFrame,
    SendFrame,
    UnsupportedProtocolVersion,
)

from test_stompman.conftest import (
    BaseMockConnection,
    EnrichedClient,
    build_dataclass,
    create_spying_connection,
    get_read_frames_with_lifespan,
)

if TYPE_CHECKING:
    from stompman.connection import AbstractConnection

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


async def test_client_connection_lifespan_adds_custom_connect_headers() -> None:
    connected_frame = build_dataclass(
        ConnectedFrame,
        headers={"version": Client.PROTOCOL_VERSION, "heart-beat": "1,1"},
    )
    connection_class, collected_frames = create_spying_connection(
        [connected_frame], [], [build_dataclass(ReceiptFrame)]
    )

    async with EnrichedClient(
        [
            ConnectionParameters(
                "localhost",
                10,
                "login",
                "passcode",
                connect_headers={"client-id": "client-1"},
            )
        ],
        connection_class=connection_class,
    ):
        await asyncio.sleep(0)

    connect_frame = collected_frames[0]
    assert isinstance(connect_frame, ConnectFrame)
    assert cast("dict[str, str]", connect_frame.headers)["client-id"] == "client-1"


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

    assert sleep_calls == [0, 1, 1, 1]
    assert write_heartbeat_mock.mock_calls == [mock.call(), mock.call(), mock.call(), mock.call()]


async def test_client_recovers_after_heartbeat_failure_when_keep_alive_enabled() -> None:  # ruff: ignore[complex-structure]
    expected_connection_count = 2
    heartbeat_failed = asyncio.Event()
    second_connection_established = asyncio.Event()
    sent_connection_numbers: list[int] = []
    closed_connection_numbers: list[int] = []
    connection_count = 0

    class RecoveringConnection:
        last_read_time: float | None = None

        def __init__(self) -> None:
            nonlocal connection_count
            connection_count += 1
            self.connection_number = connection_count
            self.closed = asyncio.Event()
            self.disconnect_sent = False
            self.read_count = 0

        @classmethod
        async def connect(
            cls,
            *,
            host: str,
            port: int,
            timeout: int,
            read_max_chunk_size: int,
            ssl: Literal[True] | SSLContext | None,
            ws_uri_path: str | None = None,
        ) -> Self:
            del host, port, timeout, read_max_chunk_size, ssl, ws_uri_path
            return cls()

        async def close(self) -> None:
            if self.closed.is_set():
                return
            closed_connection_numbers.append(self.connection_number)
            self.closed.set()

        def write_heartbeat(self) -> None:
            if self.connection_number == 1 and not heartbeat_failed.is_set():
                heartbeat_failed.set()
                raise ConnectionLostError(reason="induced heartbeat failure")

        async def write_frame(self, frame: stompman.AnyClientFrame) -> None:
            if isinstance(frame, DisconnectFrame):
                self.disconnect_sent = True
            elif isinstance(frame, SendFrame):
                sent_connection_numbers.append(self.connection_number)

        async def read_frames(self) -> AsyncGenerator[AnyServerFrame, None]:
            self.read_count += 1
            if self.read_count == 1:
                if self.connection_number == expected_connection_count:
                    second_connection_established.set()
                yield ConnectedFrame(headers={"version": Client.PROTOCOL_VERSION, "heart-beat": "1,1"})
                return
            if self.disconnect_sent:
                yield ReceiptFrame(headers={"receipt-id": "receipt-id-1"})
                return
            await self.closed.wait()
            raise ConnectionLostError(reason="closed")

    async with EnrichedClient(
        connection_class=cast("type[AbstractConnection]", RecoveringConnection),
        heartbeat=Heartbeat(will_send_interval_ms=1, want_to_receive_interval_ms=1),
        connect_retry_attempts=1,
        connect_retry_interval=0,
        write_retry_attempts=1,
        keep_alive_on_connection_failure=True,
    ) as client:
        await asyncio.wait_for(heartbeat_failed.wait(), timeout=1)
        await asyncio.wait_for(second_connection_established.wait(), timeout=1)
        await asyncio.sleep(0)

        await client.send(b"payload", destination="queue")

        assert not client._listen_task.done()
        assert sent_connection_numbers == [expected_connection_count]

    assert closed_connection_numbers == [1, expected_connection_count]


def test_make_receipt_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.undo()
    stompman.connection_lifespan._make_receipt_id()
