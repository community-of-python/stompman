# The scripted broker models wire commands explicitly and factories accept config overrides.
import asyncio
import copy
import time
from collections import deque
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field, fields
from ssl import SSLContext
from typing import TYPE_CHECKING, Any, Literal, Self, TypeVar

import pytest
import stompman
from polyfactory.factories.dataclass_factory import DataclassFactory
from stompman.connection import AbstractConnection
from stompman.core import Runtime, RuntimeConfig

if TYPE_CHECKING:
    from stompman.frames import MessageHeaders

DataclassType = TypeVar("DataclassType")


def build_dataclass(dataclass: type[DataclassType], **kwargs: Any) -> DataclassType:  # ruff: ignore[any-type]
    return DataclassFactory.create_factory(dataclass).build(**kwargs)


async def wait_until(predicate: Callable[[], bool], *, timeout: float = 1) -> None:
    async with asyncio.timeout(timeout):
        while not predicate():  # ruff: ignore[async-busy-wait]
            await asyncio.sleep(0.001)


@dataclass
class SomeError(Exception):
    pass


@dataclass(kw_only=True, slots=True)
class EnrichedClient(stompman.Client):
    servers: list[stompman.ConnectionParameters] = field(
        default_factory=lambda: [stompman.ConnectionParameters("localhost", 12345, "login", "passcode")], kw_only=False
    )


class ScriptedConnection(AbstractConnection):
    def __init__(self, broker: "ScriptedBroker", host: str) -> None:
        self.broker = broker
        self.host = host
        self.incoming: asyncio.Queue[stompman.AnyServerFrame | Exception] = asyncio.Queue()
        self.writes: list[stompman.AnyClientFrame] = []
        self.closed = False
        self.read_calls = 0
        self.heartbeats = 0
        self.subscriptions: dict[str, stompman.SubscribeFrame] = {}
        self.transactions: dict[str, list[stompman.SendFrame]] = {}

    async def close(self) -> None:
        self.closed = True
        self.transactions.clear()
        self.incoming.put_nowait(stompman.ConnectionLostError(reason="closed"))

    def write_heartbeat(self) -> None:
        if self.broker.heartbeat_failure:
            raise stompman.ConnectionLostError(reason="heartbeat failed")
        self.heartbeats += 1

    async def write_frame(self, frame: stompman.AnyClientFrame) -> None:  # ruff: ignore[complex-structure, too-many-branches]
        if self.closed:
            raise stompman.ConnectionLostError(reason="closed")
        if self.broker.fail_before is not None and self.broker.fail_before(frame, self):
            raise stompman.ConnectionLostError(reason="scripted write failure")
        self.writes.append(copy.deepcopy(frame))
        if isinstance(frame, stompman.ConnectFrame):
            response = self.broker.handshakes.get(
                self.host,
                stompman.ConnectedFrame(
                    headers={
                        "version": "1.2",
                        "heart-beat": self.broker.heartbeat,
                    }
                ),
            )
            if response is not None:
                self.incoming.put_nowait(response)
            for extra in self.broker.after_connected:
                self.incoming.put_nowait(extra)
        elif isinstance(frame, stompman.SubscribeFrame):
            assert frame.headers["id"] not in self.subscriptions
            self.subscriptions[frame.headers["id"]] = frame
        elif isinstance(frame, stompman.UnsubscribeFrame):
            self.subscriptions.pop(frame.headers["id"], None)
        elif isinstance(frame, stompman.BeginFrame):
            assert frame.headers["transaction"] not in self.transactions
            self.transactions[frame.headers["transaction"]] = []
        elif isinstance(frame, stompman.SendFrame):
            transaction = frame.headers.get("transaction")
            if transaction is not None:
                assert transaction in self.transactions, "SEND without BEGIN"
                self.transactions[transaction].append(copy.deepcopy(frame))
        elif isinstance(frame, stompman.CommitFrame):
            transaction = frame.headers["transaction"]
            assert transaction in self.transactions, "COMMIT without BEGIN"
            self.broker.committed.append(self.transactions.pop(transaction))
        elif isinstance(frame, stompman.AbortFrame):
            self.transactions.pop(frame.headers["transaction"], None)
        if self.broker.fail_after is not None and self.broker.fail_after(frame, self):
            raise stompman.ConnectionLostError(reason="connection lost after accepting frame")
        if self.broker.receipts and (receipt := frame.headers.get("receipt")):
            assert isinstance(receipt, str)
            self.incoming.put_nowait(stompman.ReceiptFrame(headers={"receipt-id": receipt}))

    async def read_frames(self) -> AsyncGenerator[stompman.AnyServerFrame, None]:
        self.read_calls += 1
        while True:
            frame = await self.incoming.get()
            if isinstance(frame, Exception):
                raise frame
            self.last_read_time = time.time()
            yield frame

    def deliver(self, subscription: str, body: bytes, *, ack_id: str | None = None) -> None:
        headers: MessageHeaders = {
            "destination": self.subscriptions[subscription].headers["destination"],
            "message-id": body.decode(),
            "subscription": subscription,
        }
        if ack_id is not None:
            headers["ack"] = ack_id
        self.incoming.put_nowait(stompman.MessageFrame(headers=headers, body=body))


class ScriptedBroker:
    def __init__(self) -> None:
        self.connections: list[ScriptedConnection] = []
        self.connect_calls = 0
        self.connect_results: deque[bool] = deque()
        self.available = True
        self.delays: dict[str, float] = {}
        self.cancelled_hosts: set[str] = set()
        self.heartbeat = "0,0"
        self.heartbeat_failure = False
        self.handshakes: dict[str, stompman.AnyServerFrame | None] = {}
        self.after_connected: list[stompman.AnyServerFrame] = []
        self.receipts = True
        self.fail_before: Callable[[stompman.AnyClientFrame, ScriptedConnection], bool] | None = None
        self.fail_after: Callable[[stompman.AnyClientFrame, ScriptedConnection], bool] | None = None
        self.committed: list[list[stompman.SendFrame]] = []
        broker = self

        class Connection(ScriptedConnection):
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
            ) -> Self | None:
                broker.connect_calls += 1
                try:
                    await asyncio.sleep(broker.delays.get(host, 0))
                except asyncio.CancelledError:
                    broker.cancelled_hosts.add(host)
                    raise
                available = broker.connect_results.popleft() if broker.connect_results else broker.available
                if not available:
                    return None
                connection = cls(broker, host)
                broker.connections.append(connection)
                return connection

        self.connection_class = Connection

    @property
    def current(self) -> ScriptedConnection:
        return self.connections[-1]

    def config(self, **kwargs: Any) -> RuntimeConfig:  # ruff: ignore[any-type]
        options: dict[str, Any] = {
            "servers": [stompman.ConnectionParameters("localhost", 12345, "login", "passcode")],
            "connection_class": self.connection_class,
            "connect_retry_interval": 0,
            "connection_confirmation_timeout": 0.1,
            "disconnect_confirmation_timeout": 0.01,
            "heartbeat": stompman.Heartbeat(0, 0),
            "no_message_restart_interval": None,
        }
        return RuntimeConfig(**(options | kwargs))

    def runtime(self, **kwargs: Any) -> Runtime:  # ruff: ignore[any-type]
        return Runtime(self.config(**kwargs))

    def client(self, **kwargs: Any) -> stompman.Client:  # ruff: ignore[any-type]
        config = self.config(**kwargs)
        return EnrichedClient(**{item.name: getattr(config, item.name) for item in fields(config)})


@pytest.fixture
def broker() -> ScriptedBroker:
    return ScriptedBroker()
