import asyncio
from collections.abc import AsyncGenerator
from ssl import SSLContext
from typing import Annotated, ClassVar, Literal, NoReturn, Self, cast
from unittest import mock

import pytest
import stompman
from faststream import Context
from faststream_stomp import StompBroker
from stompman._compat import LegacyOptions, LegacyTransportFactory
from stompman.connection import AbstractConnection
from stompman.core.config import RuntimeConfig
from stompman.core.runtime import Runtime
from stompman.frames import (
    AbortFrame,
    AckFrame,
    AnyClientFrame,
    AnyServerFrame,
    BeginFrame,
    CommitFrame,
    ConnectedFrame,
    ConnectFrame,
    DisconnectFrame,
    MessageFrame,
    MessageHeaders,
    ReceiptFrame,
    SendFrame,
    SubscribeFrame,
    UnsubscribeFrame,
)

pytestmark = pytest.mark.anyio


class LoopbackConnection(AbstractConnection):
    instances: ClassVar[list["LoopbackConnection"]] = []

    def __init__(self) -> None:
        self.incoming: asyncio.Queue[AnyServerFrame | None] = asyncio.Queue()
        self.frames: list[AnyClientFrame] = []
        self.subscriptions: dict[str, str] = {}
        self.transactions: dict[str, list[SendFrame]] = {}
        self.closed = False
        self._next_message_id = 0

    @classmethod
    async def connect(
        cls,
        *,
        host: str,
        port: int,
        timeout: float,
        read_max_chunk_size: int,
        ssl: Literal[True] | SSLContext | None,
        ws_uri_path: str | None = None,
    ) -> Self:
        connection = cls()
        cls.instances.append(connection)
        return connection

    async def close(self) -> None:
        self.closed = True
        self.incoming.put_nowait(None)

    def write_heartbeat(self) -> None: ...

    def _deliver(self, frame: SendFrame) -> None:
        if subscription_id := self.subscriptions.get(frame.headers["destination"]):
            self._next_message_id += 1
            self.incoming.put_nowait(
                MessageFrame(
                    headers=cast(
                        "MessageHeaders",
                        frame.headers
                        | {
                            "subscription": subscription_id,
                            "message-id": str(self._next_message_id),
                            "ack": str(self._next_message_id),
                        },
                    ),
                    body=frame.body,
                )
            )

    async def write_frame(self, frame: AnyClientFrame) -> None:  # ruff: ignore[complex-structure]
        self.frames.append(frame)
        match frame:
            case ConnectFrame():
                self.incoming.put_nowait(ConnectedFrame(headers={"version": "1.2", "heart-beat": "0,0"}))
            case SubscribeFrame():
                self.subscriptions[frame.headers["destination"]] = frame.headers["id"]
            case UnsubscribeFrame():
                self.subscriptions = {
                    destination: subscription_id
                    for destination, subscription_id in self.subscriptions.items()
                    if subscription_id != frame.headers["id"]
                }
            case BeginFrame():
                self.transactions[frame.headers["transaction"]] = []
            case SendFrame():
                if transaction_id := frame.headers.get("transaction"):
                    self.transactions[transaction_id].append(frame)
                else:
                    self._deliver(frame)
            case CommitFrame():
                for sent_frame in self.transactions.pop(frame.headers["transaction"]):
                    self._deliver(sent_frame)
            case AbortFrame():
                self.transactions.pop(frame.headers["transaction"])
        if receipt_id := frame.headers.get("receipt"):
            self.incoming.put_nowait(ReceiptFrame(headers={"receipt-id": str(receipt_id)}))

    async def read_frames(self) -> AsyncGenerator[AnyServerFrame, None]:
        while True:
            frame = await self.incoming.get()
            if frame is None:
                raise stompman.ConnectionLostError(reason="closed")
            yield frame


@pytest.fixture
def connection_class() -> type[LoopbackConnection]:
    class Connection(LoopbackConnection):
        instances: ClassVar[list[LoopbackConnection]] = []

    return Connection


def make_config(connection_class: type[LoopbackConnection]) -> RuntimeConfig:
    return LegacyOptions(
        [stompman.ConnectionParameters("broker.example", 61616, "guest", "guest")],
        connection_class=connection_class,
        heartbeat=stompman.Heartbeat(0, 0),
        no_message_restart_interval=None,
        connect_retry_attempts=1,
        connect_retry_interval=0,
    ).to_config()


async def test_legacy_client_only_supplies_configuration(
    monkeypatch: pytest.MonkeyPatch, connection_class: type[LoopbackConnection]
) -> None:
    config = make_config(connection_class)
    client = stompman.Client(
        [stompman.ConnectionParameters("broker.example", 61616, "guest", "guest")],
        connection_class=connection_class,
        heartbeat=config.connection.heartbeat,
        no_message_restart_interval=None,
    )

    def forbidden(*args: object, **kwargs: object) -> NoReturn:
        msg = "FastStream must execute the core directly"
        raise AssertionError(msg)

    for method_name in (
        "__aenter__",
        "__aexit__",
        "send",
        "begin",
        "subscribe",
        "subscribe_with_manual_ack",
        "is_alive",
    ):
        monkeypatch.setattr(stompman.Client, method_name, forbidden)

    broker = StompBroker(client)
    assert broker.runtime is not client.core
    received: list[str] = []
    delivered = asyncio.Event()
    expected = ["one", "two", "three"]

    @broker.subscriber("events")
    async def consume(
        body: str, frame: Annotated[stompman.AckableMessageFrame, Context("message.raw_message")]
    ) -> None:
        assert type(frame) is stompman.AckableMessageFrame
        await frame.ack()
        received.append(body)
        if len(received) == len(expected):
            delivered.set()

    async with broker:
        await broker.start()
        await broker.publish(expected[0], "events")
        await broker.publish_batch(*expected[1:], destination="events")
        await asyncio.wait_for(delivered.wait(), timeout=1)
        assert await broker.ping()
    assert sorted(received) == sorted(expected)
    assert connection_class.instances[-1].closed
    assert sum(isinstance(frame, AckFrame) for frame in connection_class.instances[-1].frames) == len(expected)
    assert broker.runtime.status.state == "closed"


async def test_runtime_automatic_reply_preserves_metadata(connection_class: type[LoopbackConnection]) -> None:
    broker = StompBroker(make_config(connection_class), transport_factory=LegacyTransportFactory(connection_class, {}))
    delivered = asyncio.Event()
    replies: list[stompman.AckableMessageFrame] = []

    @broker.subscriber("requests", reply_add_content_length=False)
    def respond(body: str) -> str:
        return f"reply:{body}"

    @broker.subscriber("replies")
    async def consume(frame: Annotated[stompman.AckableMessageFrame, Context("message.raw_message")]) -> None:
        replies.append(frame)
        await frame.ack()
        delivered.set()

    async with broker:
        await broker.start()
        await broker.publish("request", "requests", headers={"reply-to": "replies"}, correlation_id="correlation")
        await asyncio.wait_for(delivered.wait(), timeout=1)

    assert len(replies) == 1
    assert replies[0].body == b"reply:request"
    assert replies[0].headers.get("correlation-id") == "correlation"
    assert "content-length" not in replies[0].headers


@pytest.mark.parametrize("source", ["config", "runtime"])
async def test_native_runtime_lifecycle_can_restart(connection_class: type[LoopbackConnection], source: str) -> None:
    config = make_config(connection_class)
    runtime = Runtime(config, transport_factory=LegacyTransportFactory(connection_class, {}))
    broker = StompBroker(
        runtime if source == "runtime" else config, transport_factory=LegacyTransportFactory(connection_class, {})
    )
    if source == "runtime":
        assert broker.runtime is runtime

    @broker.subscriber("events")
    def consume(body: str) -> None: ...

    for _ in range(2):
        await broker.start()
        await broker.start()
        assert await broker.ping()
        connection = connection_class.instances[-1]
        assert sum(isinstance(frame, SubscribeFrame) for frame in connection.frames) == 1
        await broker.stop()
        assert not await broker.ping()
        assert connection.closed
        assert any(isinstance(frame, DisconnectFrame) for frame in connection.frames)


async def test_failed_connect_can_be_retried(connection_class: type[LoopbackConnection]) -> None:
    runtime = Runtime(make_config(connection_class), transport_factory=LegacyTransportFactory(connection_class, {}))
    broker = StompBroker(runtime)
    with (
        mock.patch.object(runtime, "start", side_effect=RuntimeError("connect failed")),
        pytest.raises(RuntimeError, match="connect failed"),
    ):
        await broker.connect()
    async with broker:
        assert await broker.ping()


async def test_subscriber_start_failure_closes_runtime(connection_class: type[LoopbackConnection]) -> None:
    runtime = Runtime(make_config(connection_class), transport_factory=LegacyTransportFactory(connection_class, {}))
    broker = StompBroker(runtime)

    @broker.subscriber("events")
    def consume(body: str) -> None: ...

    with (
        mock.patch.object(runtime, "subscribe", side_effect=RuntimeError("subscribe failed")),
        pytest.raises((RuntimeError, ExceptionGroup)),
    ):
        await broker.start()
    assert runtime.status.state == "closed"
    assert connection_class.instances[-1].closed
    assert not broker.running


def test_servers_constructor(connection_class: type[LoopbackConnection]) -> None:
    config = make_config(connection_class)
    servers = [stompman.ConnectionParameters("broker.example", 61616, "guest", "guest")]
    broker = StompBroker(servers=servers)
    assert broker.runtime.config.servers == config.servers
    with pytest.raises(TypeError, match="not both"):
        StompBroker(config, servers=servers)


async def test_graceful_stop_settles_running_handler_before_unsubscribe(
    connection_class: type[LoopbackConnection],
) -> None:
    broker = StompBroker(make_config(connection_class), transport_factory=LegacyTransportFactory(connection_class, {}))
    entered = asyncio.Event()
    release = asyncio.Event()

    @broker.subscriber("q")
    async def handle(body: str) -> None:
        entered.set()
        await release.wait()

    await broker.start()
    await broker.publish("one", "q")
    await entered.wait()
    stop = asyncio.create_task(broker.stop())
    await asyncio.sleep(0)
    release.set()
    await stop
    commands = [type(frame) for frame in connection_class.instances[0].frames]
    assert commands.index(AckFrame) < commands.index(UnsubscribeFrame)
    assert connection_class.instances[0].closed


async def test_stop_cancels_handlers_after_faststream_grace_timeout(
    connection_class: type[LoopbackConnection],
) -> None:
    broker = StompBroker(
        make_config(connection_class),
        transport_factory=LegacyTransportFactory(connection_class, {}),
        graceful_timeout=0.001,
    )
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    @broker.subscriber("q")
    async def handle(body: str) -> None:
        entered.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()

    await broker.start()
    await broker.publish("one", "q")
    await entered.wait()
    await asyncio.wait_for(broker.stop(), timeout=1)
    assert cancelled.is_set()
    assert connection_class.instances[0].closed
