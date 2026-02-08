import asyncio
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from ssl import SSLContext
from typing import Any, Literal, Self, TypeVar

import pytest
import stompman
from polyfactory.factories.dataclass_factory import DataclassFactory
from stompman.config import Heartbeat
from stompman.connection import AbstractConnection
from stompman.connection_lifespan import AbstractConnectionLifespan, EstablishedConnectionResult
from stompman.connection_manager import ConnectionManager


@pytest.fixture
def mock_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    original_sleep = asyncio.sleep
    monkeypatch.setattr("asyncio.sleep", lambda _: original_sleep(0))


async def noop_message_handler(frame: stompman.MessageFrame) -> None: ...


def noop_error_handler(exception: Exception, frame: stompman.MessageFrame) -> None: ...


class BaseMockConnection(AbstractConnection):
    @classmethod
    async def connect(
        cls,
        *,
        host: str,
        port: int,
        timeout: int,
        read_max_chunk_size: int,
        ssl: Literal[True] | SSLContext | None,
        uri_path: str = "",
    ) -> Self | None:
        return cls()

    async def close(self) -> None: ...
    def write_heartbeat(self) -> None: ...
    async def write_frame(self, frame: stompman.AnyClientFrame) -> None: ...
    @staticmethod
    async def read_frames() -> AsyncGenerator[stompman.AnyServerFrame, None]:  # pragma: no cover
        await asyncio.Future()
        yield stompman.HeartbeatFrame()


@dataclass(kw_only=True, slots=True)
class EnrichedClient(stompman.Client):
    servers: list[stompman.ConnectionParameters] = field(
        default_factory=lambda: [stompman.ConnectionParameters("localhost", 12345, "login", "passcode")], kw_only=False
    )


@dataclass(frozen=True, kw_only=True, slots=True)
class NoopLifespan(AbstractConnectionLifespan):
    connection: AbstractConnection
    connection_parameters: stompman.ConnectionParameters
    set_heartbeat_interval: Callable[[Heartbeat], Any]

    async def enter(self) -> EstablishedConnectionResult | stompman.StompProtocolConnectionIssue:
        return EstablishedConnectionResult(server_heartbeat=stompman.Heartbeat(1000, 1000))

    async def exit(self) -> None: ...


@dataclass(kw_only=True, slots=True)
class EnrichedConnectionManager(ConnectionManager):
    servers: list[stompman.ConnectionParameters] = field(
        default_factory=lambda: [stompman.ConnectionParameters("localhost", 12345, "login", "passcode")]
    )
    lifespan_factory: stompman.connection_lifespan.ConnectionLifespanFactory = field(default=NoopLifespan)
    connect_retry_attempts: int = 3
    connect_retry_interval: int = 1
    connect_timeout: int = 3
    read_max_chunk_size: int = 5
    write_retry_attempts: int = 3
    ssl: Literal[True] | SSLContext | None = None
    check_server_alive_interval_factor: int = 3


DataclassType = TypeVar("DataclassType")


def build_dataclass(dataclass: type[DataclassType], **kwargs: Any) -> DataclassType:  # noqa: ANN401
    return DataclassFactory.create_factory(dataclass).build(**kwargs)


@dataclass
class SomeError(Exception):
    @classmethod
    async def raise_after_tick(cls) -> None:
        await asyncio.sleep(0)
        raise cls


def create_spying_connection(
    *read_frames_yields: list[stompman.AnyServerFrame],
) -> tuple[type[AbstractConnection], list[stompman.AnyClientFrame | stompman.AnyServerFrame]]:
    class BaseCollectingConnection(BaseMockConnection):
        @staticmethod
        async def write_frame(frame: stompman.AnyClientFrame) -> None:
            collected_frames.append(frame)

        @staticmethod
        async def read_frames() -> AsyncGenerator[stompman.AnyServerFrame, None]:
            for frame in next(read_frames_iterator):
                collected_frames.append(frame)
                yield frame
            await asyncio.Future()

    read_frames_iterator = iter(read_frames_yields)
    collected_frames: list[stompman.AnyClientFrame | stompman.AnyServerFrame] = []
    return BaseCollectingConnection, collected_frames


CONNECT_FRAME = stompman.ConnectFrame(
    headers={
        "accept-version": stompman.Client.PROTOCOL_VERSION,
        "heart-beat": "1000,1000",
        "host": "localhost",
        "login": "login",
        "passcode": "passcode",
    },
)
CONNECTED_FRAME = stompman.ConnectedFrame(
    headers={"version": stompman.Client.PROTOCOL_VERSION, "heart-beat": "1000,1000"}
)


@pytest.fixture(autouse=True)
def _mock_receipt_id(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stompman.connection_lifespan, "_make_receipt_id", lambda: "receipt-id-1")


def get_read_frames_with_lifespan(*read_frames: list[stompman.AnyServerFrame]) -> list[list[stompman.AnyServerFrame]]:
    return [
        [CONNECTED_FRAME],
        *read_frames,
        [stompman.ReceiptFrame(headers={"receipt-id": "receipt-id-1"})],
    ]


def enrich_expected_frames(
    *expected_frames: stompman.AnyClientFrame | stompman.AnyServerFrame,
) -> list[stompman.AnyClientFrame | stompman.AnyServerFrame]:
    return [
        CONNECT_FRAME,
        CONNECTED_FRAME,
        *expected_frames,
        stompman.DisconnectFrame(headers={"receipt": "receipt-id-1"}),
        stompman.ReceiptFrame(headers={"receipt-id": "receipt-id-1"}),
    ]
