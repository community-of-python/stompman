import asyncio
from collections.abc import AsyncGenerator, Callable, Coroutine
from contextlib import asynccontextmanager
from ssl import SSLContext
from types import TracebackType
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Literal, NoReturn, Self, cast
from unittest import mock

import pytest
import stompman
from faststream import Context
from faststream_stomp import StompBroker
from stompman._compat import LegacyTransportFactory
from stompman.connection import AbstractConnection
from stompman.core.config import ConnectionSettings, Heartbeat, RecoveryPolicy, RuntimeConfig, Server
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

if TYPE_CHECKING:
    from faststream_stomp.publisher import StompProducer

pytestmark = pytest.mark.anyio


class LoopbackConnection(AbstractConnection):
    instances: ClassVar[list["LoopbackConnection"]] = []
    operation_receipts: ClassVar[bool] = True

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
        if (receipt_id := frame.headers.get("receipt")) and (
            self.operation_receipts or isinstance(frame, DisconnectFrame)
        ):
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
    return RuntimeConfig(
        (Server("broker.example", 61616, "guest", "guest"),),
        connection=ConnectionSettings(heartbeat=Heartbeat(0, 0)),
        recovery=RecoveryPolicy(attempts=1, delay=0),
    )


def make_legacy_client(connection_class: type[LoopbackConnection]) -> stompman.Client:
    return stompman.Client(
        [stompman.ConnectionParameters("broker.example", 61616, "guest", "guest")],
        connection_class=connection_class,
        heartbeat=stompman.Heartbeat(0, 0),
        no_message_restart_interval=None,
        connect_retry_attempts=1,
        connect_retry_interval=0,
    )


@pytest.mark.parametrize("source", ["config", "runtime"])
async def test_native_client_execution_is_independent_of_legacy_client(
    monkeypatch: pytest.MonkeyPatch, connection_class: type[LoopbackConnection], source: str
) -> None:
    config = make_config(connection_class)
    transport = LegacyTransportFactory(connection_class, {}, timeout=2)

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

    broker = StompBroker(
        Runtime(config, transport_factory=transport) if source == "runtime" else config,
        transport_factory=transport,
    )
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
    assert all(
        "receipt" in frame.headers
        for frame in connection_class.instances[-1].frames
        if isinstance(frame, (SubscribeFrame, SendFrame, BeginFrame, CommitFrame, AckFrame, UnsubscribeFrame))
    )


class InstrumentedClient(stompman.Client):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.calls: list[str] = []

    async def __aenter__(self) -> Self:
        self.calls.append("enter")
        return await super().__aenter__()

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc_value: BaseException | None, traceback: TracebackType | None
    ) -> None:
        self.calls.append("exit")
        await super().__aexit__(exc_type, exc_value, traceback)

    async def send(
        self,
        body: bytes,
        destination: str,
        *,
        content_type: str | None = None,
        add_content_length: bool = True,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.calls.append("send")
        await super().send(
            body, destination, content_type=content_type, add_content_length=add_content_length, headers=headers
        )

    @asynccontextmanager
    async def begin(self) -> AsyncGenerator[stompman.Transaction, None]:
        self.calls.append("begin")
        async with super().begin() as transaction:
            yield transaction

    async def subscribe_with_manual_ack(
        self,
        destination: str,
        handler: Callable[[stompman.AckableMessageFrame], Coroutine[Any, Any, Any]],
        *,
        ack: stompman.AckMode = "client-individual",
        headers: dict[str, str] | None = None,
        receipt_timeout: float | None = None,
        on_subscription_error: Callable[[stompman.SubscriptionError], Any] | None = None,
    ) -> stompman.ManualAckSubscription:
        self.calls.append("subscribe")
        return await super().subscribe_with_manual_ack(
            destination,
            handler,
            ack=ack,
            headers=headers,
            receipt_timeout=receipt_timeout,
            on_subscription_error=on_subscription_error,
        )

    def is_alive(self) -> bool:
        self.calls.append("is_alive")
        return super().is_alive()

    def extension(self) -> list[str]:
        return self.calls


class ClientProxy:
    def __init__(self, client: InstrumentedClient) -> None:
        self.client = client

    def __getattr__(self, name: str) -> Any:  # ruff: ignore[any-type]
        return getattr(self.client, name)


@pytest.mark.parametrize("source", ["client", "proxy"])
async def test_explicit_legacy_client_preserves_identity_hooks_and_unconfirmed_defaults(
    connection_class: type[LoopbackConnection], source: str
) -> None:
    connection_class.operation_receipts = False
    client = InstrumentedClient(
        [stompman.ConnectionParameters("broker.example", 61616, "guest", "guest")],
        connection_class=connection_class,
        heartbeat=stompman.Heartbeat(0, 0),
        no_message_restart_interval=None,
    )
    injected = client if source == "client" else cast("stompman.Client", ClientProxy(client))
    broker = StompBroker(injected)
    assert broker.config.broker_config.client is injected
    assert cast("StompProducer", broker.config.producer).client is injected
    received: list[str] = []
    delivered = asyncio.Event()

    @broker.subscriber("events")
    async def consume(
        body: str, frame: Annotated[stompman.AckableMessageFrame, Context("message.raw_message")]
    ) -> None:
        assert type(frame) is stompman.AckableMessageFrame
        await frame.ack()
        received.append(body)
        if len(received) == 3:
            delivered.set()

    async with broker:
        assert await broker.connect() is injected
        assert cast("InstrumentedClient", await broker.connect()).extension() is client.calls
        await broker.start()
        assert client.core.is_alive()
        await broker.publish("one", "events")
        await broker.publish_batch("two", "three", destination="events")
        await asyncio.wait_for(delivered.wait(), timeout=1)
        assert await broker.ping()

    assert sorted(received) == ["one", "three", "two"]
    assert client.calls == ["enter", "subscribe", "send", "begin", "is_alive", "exit"]
    assert client.core.status.state == "closed"
    assert connection_class.instances[-1].closed
    assert all(
        "receipt" not in frame.headers
        for frame in connection_class.instances[-1].frames
        if isinstance(frame, (SubscribeFrame, SendFrame, BeginFrame, CommitFrame, AckFrame, UnsubscribeFrame))
    )


async def test_explicit_legacy_mock_is_used_without_native_configuration() -> None:
    client_mock = mock.create_autospec(stompman.Client, instance=True)
    client_mock.servers = [stompman.ConnectionParameters("broker.example", 61616, "guest", "guest")]
    subscription = mock.Mock(unsubscribe=mock.AsyncMock())
    client_mock.subscribe_with_manual_ack.return_value = subscription
    client_mock.is_alive.return_value = True
    client = cast("stompman.Client", client_mock)
    broker = StompBroker(client)

    @broker.subscriber("events")
    def consume(body: str) -> None: ...

    async with broker:
        assert await broker.connect() is client
        await broker.start()
        await broker.publish("one", "events")
        assert await broker.ping()

    client_mock.__aenter__.assert_awaited_once_with()
    client_mock.__aexit__.assert_awaited_once_with(None, None, None)
    client_mock.subscribe_with_manual_ack.assert_awaited_once()
    client_mock.send.assert_awaited_once()
    client_mock.is_alive.assert_called_once()
    subscription.unsubscribe.assert_awaited_once_with()


async def test_legacy_exit_receives_subscription_stop_failure() -> None:
    client = mock.create_autospec(stompman.Client, instance=True)
    client.servers = [stompman.ConnectionParameters("broker.example", 61616, "guest", "guest")]
    failure = RuntimeError("unsubscribe failed")
    client.subscribe_with_manual_ack.return_value = mock.Mock(unsubscribe=mock.AsyncMock(side_effect=failure))
    broker = StompBroker(client)

    @broker.subscriber("events")
    def consume(body: str) -> None: ...

    await broker.start()
    with pytest.raises(RuntimeError, match="unsubscribe failed"):
        await broker.stop()
    assert client.__aexit__.await_args.args[:2] == (RuntimeError, failure)
    assert broker._connection is None
    assert not broker.running


async def test_runtime_automatic_reply_preserves_metadata(connection_class: type[LoopbackConnection]) -> None:
    broker = StompBroker(
        make_config(connection_class), transport_factory=LegacyTransportFactory(connection_class, {}, timeout=2)
    )
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


@pytest.mark.parametrize("source", ["config", "runtime", "legacy"])
async def test_runtime_lifecycle_can_restart(connection_class: type[LoopbackConnection], source: str) -> None:
    config = make_config(connection_class)
    runtime = Runtime(config, transport_factory=LegacyTransportFactory(connection_class, {}, timeout=2))
    broker = StompBroker(
        runtime if source == "runtime" else config,
        transport_factory=LegacyTransportFactory(connection_class, {}, timeout=2),
    )
    if source == "legacy":
        broker = StompBroker(make_legacy_client(connection_class))
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
    runtime = Runtime(
        make_config(connection_class), transport_factory=LegacyTransportFactory(connection_class, {}, timeout=2)
    )
    broker = StompBroker(runtime)
    with (
        mock.patch.object(runtime, "start", side_effect=RuntimeError("connect failed")),
        pytest.raises(RuntimeError, match="connect failed"),
    ):
        await broker.connect()
    async with broker:
        assert await broker.ping()


async def test_subscriber_start_failure_closes_runtime(connection_class: type[LoopbackConnection]) -> None:
    runtime = Runtime(
        make_config(connection_class), transport_factory=LegacyTransportFactory(connection_class, {}, timeout=2)
    )
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


@pytest.mark.parametrize("legacy", [False, True])
async def test_graceful_stop_settles_running_handler_before_unsubscribe(
    connection_class: type[LoopbackConnection], legacy: bool
) -> None:
    broker = StompBroker(
        make_legacy_client(connection_class) if legacy else make_config(connection_class),
        transport_factory=LegacyTransportFactory(connection_class, {}, timeout=2),
    )
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


@pytest.mark.parametrize("legacy", [False, True])
async def test_stop_cancels_handlers_after_faststream_grace_timeout(
    connection_class: type[LoopbackConnection], legacy: bool
) -> None:
    broker = StompBroker(
        make_legacy_client(connection_class) if legacy else make_config(connection_class),
        transport_factory=LegacyTransportFactory(connection_class, {}, timeout=2),
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
