"""Consumer-shaped checks for the pre-core facade and its extension points."""

import asyncio
from dataclasses import dataclass, fields, replace
from functools import partial
from ssl import SSLContext
from typing import Literal, Self
from unittest.mock import AsyncMock

import pytest
import stompman
from stompman.connection import AbstractConnection, Connection
from stompman.connection_lifespan import ConnectionLifespan, EstablishedConnectionResult
from stompman.connection_manager import ConnectionRestoration
from stompman.core import Paused, Unbounded
from stompman.errors import AllServersUnavailable
from stompman.subscription import ActiveSubscriptions

from test_stompman.conftest import ScriptedBroker, ScriptedConnection, wait_until

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(10)]


class InstrumentedClient(stompman.Client):
    async def send(
        self,
        body: bytes,
        destination: str,
        *,
        content_type: str | None = None,
        add_content_length: bool = True,
        headers: dict[str, str] | None = None,
    ) -> None:
        await super().send(
            body, destination, content_type=content_type, add_content_length=add_content_length, headers=headers
        )


class InstrumentedConnection(Connection):
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
    ) -> Self | None:
        return await super().connect(
            host=host,
            port=port,
            timeout=timeout,
            read_max_chunk_size=read_max_chunk_size,
            ssl=ssl,
            ws_uri_path=ws_uri_path,
        )


async def noop(frame: stompman.MessageFrame) -> None:
    pass


async def test_original_dataclass_constructors_and_extension_fields(broker: ScriptedBroker) -> None:
    client = broker.client()

    @dataclass(kw_only=True, slots=True)
    class ConsumerSubscription(stompman.ManualAckSubscription):
        label: str

    subscription = ConsumerSubscription(
        destination="initial",
        headers=None,
        ack="client-individual",
        handler=noop,
        _connection_manager=client._connection_manager,
        _active_subscriptions=ActiveSubscriptions(),
        label="consumer",
    )
    replacement = replace(subscription, destination="replacement")
    assert replacement.destination == "replacement"
    assert replacement.label == "consumer"
    assert {field.name for field in fields(subscription)} >= {"id", "destination", "headers", "ack", "handler"}
    owner = AsyncMock(spec=stompman.ManualAckSubscription)
    frame = stompman.AckableMessageFrame(
        headers={"destination": "q", "message-id": "m", "subscription": "s"},
        body=b"body",
        _subscription=owner,
        _received_at_reconnection_count=4,
    )
    changed = replace(frame, body=b"changed")
    await changed.ack()
    owner._ack.assert_awaited_once_with(changed, received_at_reconnection_count=4)
    transaction = stompman.Transaction(_connection_manager=client._connection_manager, _active_transactions=set())
    transactions = {transaction}
    assert transactions.pop() is transaction
    journal = transaction.sent_frames
    assert transaction.sent_frames is journal


async def test_begin_is_lazy_and_recreates_decorated_transactions(broker: ScriptedBroker) -> None:
    client = broker.client()
    context = client.begin()

    @client.begin()
    async def publish() -> None:
        await client.send(b"outside", "q")

    async with client:
        async with context as transaction:
            assert transaction in client._active_transactions
            await transaction.send(b"inside", "q")
        await publish()
        await publish()
        assert client._active_transactions == set()
    begins = [frame.headers["transaction"] for frame in broker.current.writes if isinstance(frame, stompman.BeginFrame)]
    assert len(begins) == len(set(begins)) == 3


async def test_mutable_subscription_metadata_and_callbacks_remain_live(broker: ScriptedBroker) -> None:
    calls: list[str] = []
    headers = {"selector": "before"}
    client = broker.client(on_error_frame=lambda frame: calls.append("old error"))
    async with client:
        subscription = await client.subscribe_with_manual_ack("old", noop, ack="auto", headers=headers)
        assert subscription.headers is headers

        async def replacement(frame: stompman.AckableMessageFrame) -> None:
            calls.append(frame.body.decode())

        subscription.handler = replacement
        client.on_error_frame = lambda frame: calls.append("new error")
        broker.current.deliver(subscription.id, b"replacement")
        broker.current.incoming.put_nowait(stompman.ErrorFrame(headers={"message": "test"}, body=b"error"))
        await wait_until(lambda: len(calls) == 2)
        headers["selector"] = "after"
        subscription.destination = "new"
        subscription.receipt_timeout = 0.2
        await client.core.reconnect()
        installed = broker.current.subscriptions[subscription.id]
        assert installed.headers["destination"] == "new"
        assert dict(installed.headers)["selector"] == "after"
        assert "receipt" in installed.headers
        await subscription.unsubscribe()
    assert calls in (["new error", "replacement"], ["replacement", "new error"])


async def test_replaced_suppression_policy_and_subscription_error_callback(broker: ScriptedBroker) -> None:
    callbacks: list[str] = []
    async with broker.client() as client:
        subscription = await client.subscribe(
            "q",
            noop,
            on_suppressed_exception=lambda error, frame: callbacks.append("old"),
        )

        async def fail(frame: stompman.MessageFrame) -> None:
            raise LookupError(frame.body)

        subscription.handler = fail
        subscription.suppressed_exception_classes = (LookupError,)
        subscription.on_suppressed_exception = lambda error, frame: callbacks.append("new")
        broker.current.deliver(subscription.id, b"failed", ack_id="ack")
        await wait_until(lambda: callbacks == ["new"])
        assert any(isinstance(frame, stompman.NackFrame) for frame in broker.current.writes)
        subscription.receipt_timeout = 0.001
        subscription.on_subscription_error = lambda error: callbacks.append(error.reason)
        broker.receipts = False
        await client.core.reconnect()
        assert callbacks == ["new", "timeout"]
        assert client._active_subscriptions.get_all() == []


@pytest.mark.parametrize("attempts", [1, 3])
@pytest.mark.parametrize("operation", ["send", "subscribe", "transaction"])
async def test_exhausted_writes_keep_the_legacy_exception(
    broker: ScriptedBroker, attempts: int, operation: str
) -> None:
    async with broker.client(write_retry_attempts=attempts) as client:
        broker.fail_before = lambda frame, connection: isinstance(frame, (stompman.SendFrame, stompman.SubscribeFrame))
        with pytest.raises(stompman.FailedAllWriteAttemptsError) as failure:
            if operation == "send":
                await client.send(b"body", "q")
            elif operation == "subscribe":
                await client.subscribe_with_manual_ack("q", noop)
            else:
                async with client.begin() as transaction:
                    await transaction.send(b"body", "q")
        assert failure.value.retry_attempts == attempts


async def test_diagnostics_preserve_original_server_objects_and_grouping(broker: ScriptedBroker) -> None:
    servers = [
        stompman.ConnectionParameters("first", 1234, "u", "p", ws_uri_path="/first"),
        stompman.ConnectionParameters("second", 1235, "u", "p", ws_uri_path="/second"),
    ]
    broker.available = False
    client = broker.client(servers=servers, connect_retry_attempts=2)
    with pytest.raises(stompman.FailedAllConnectAttemptsError) as failure:
        await client.__aenter__()
    assert len(failure.value.issues) == 2
    for issue in failure.value.issues:
        assert isinstance(issue, AllServersUnavailable)
        assert issue.servers is servers
        assert issue.servers[0] is servers[0]
    diagnostic = AllServersUnavailable(servers=servers, timeout=1)
    assert diagnostic.servers[1].ws_uri_path == "/second"


@pytest.mark.parametrize("option", ["connect_timeout", "connection_confirmation_timeout", "connect_retry_attempts"])
async def test_expired_legacy_configuration_is_lazy_and_uses_legacy_failures(
    broker: ScriptedBroker, option: str
) -> None:
    if option == "connect_timeout":
        broker.available = False
    client = broker.client(**{option: 0})
    with pytest.raises(stompman.FailedAllConnectAttemptsError):
        await client.__aenter__()
    empty = stompman.Client([])
    with pytest.raises(stompman.FailedAllConnectAttemptsError):
        await empty.__aenter__()


async def test_legacy_disabled_and_unbounded_policies(broker: ScriptedBroker) -> None:
    client = broker.client(max_concurrent_handlers=None, disconnect_confirmation_timeout=0, write_retry_attempts=0)
    async with client:
        assert isinstance(client.core.config.delivery.concurrency, Unbounded)
        assert isinstance(client.core.config.delivery.pending_messages, Unbounded)
        assert isinstance(client.core.config.delivery.pending_bytes, Unbounded)
        with pytest.raises(stompman.FailedAllWriteAttemptsError):
            await client.send(b"body", "q")
    assert "receipt" not in broker.current.writes[-1].headers
    async with broker.client(max_concurrent_handlers=0) as paused:
        assert isinstance(paused.core.config.delivery.concurrency, Paused)


async def test_protocol_override_and_server_list_mutation(broker: ScriptedBroker) -> None:
    class Client11(stompman.Client):
        PROTOCOL_VERSION = "1.1"

    servers = [stompman.ConnectionParameters("first", 1234, "u", "p")]
    broker.handshakes = {
        host: stompman.ConnectedFrame(headers={"version": "1.1", "heart-beat": "0,0"}) for host in ("first", "second")
    }
    async with Client11(**broker.options(servers=servers)) as client:
        connect = broker.current.writes[0]
        assert isinstance(connect, stompman.ConnectFrame)
        assert connect.headers["accept-version"] == "1.1"
        servers[:] = [stompman.ConnectionParameters("second", 1234, "u", "p")]
        await client.core.reconnect()
        assert broker.current.host == "second"


async def test_standalone_manager_honors_lifespan_reader_and_connection_hooks(broker: ScriptedBroker) -> None:
    events: list[str] = []
    template = broker.client()._connection_manager

    class Lifespan(ConnectionLifespan):
        async def enter(self) -> EstablishedConnectionResult | stompman.StompProtocolConnectionIssue:
            events.append("enter")
            return await super().enter()

        async def exit(self) -> None:
            events.append("exit")
            await super().exit()

    def restore(connection: AbstractConnection) -> ConnectionRestoration:
        async def run() -> None:
            events.append("restore")
            await connection.write_frame(
                stompman.SendFrame.build(
                    body=b"restored",
                    destination="q",
                    transaction=None,
                    content_type=None,
                    add_content_length=True,
                    headers=None,
                )
            )

        return run

    manager = replace(
        template,
        lifespan_factory=partial(
            Lifespan,
            protocol_version="1.2",
            client_heartbeat=stompman.Heartbeat(0, 0),
            connection_confirmation_timeout=1,
            disconnect_confirmation_timeout=1,
            active_subscriptions=ActiveSubscriptions(),
        ),
        restore_connection=restore,
        on_connection_lost=lambda connection: events.append("lost"),
    )
    async with manager:
        assert events == ["enter", "restore"]
        reader = manager.read_frames_reconnecting()
        receipt_task = asyncio.create_task(anext(reader))
        await asyncio.sleep(0)
        await manager.write_frame_reconnecting(
            stompman.SendFrame.build(
                body=b"body",
                destination="q",
                transaction=None,
                content_type=None,
                add_content_length=True,
                headers={"receipt": "receipt"},
            )
        )
        frame, epoch = await receipt_task
        assert isinstance(frame, stompman.ReceiptFrame)
        assert frame.headers["receipt-id"] == "receipt"
        assert epoch == 0
        await manager.write_heartbeat_reconnecting()
        assert broker.current.heartbeats == 1
        first = broker.current
        first.incoming.put_nowait(stompman.ConnectionLostError(reason="test"))
        await wait_until(lambda: len(broker.connections) == 2)
        await manager._get_restored_connection_state()
        assert events[:4] == ["enter", "restore", "lost", "enter"]
        assert events.count("restore") == 2
        assert manager._active_connection_state is not None
        await reader.aclose()
    assert events[-1] == "exit"
    assert all(connection.closed for connection in broker.connections)


async def test_mutable_transaction_journal_is_used_for_restoration(broker: ScriptedBroker) -> None:
    async with broker.client() as client, client.begin() as transaction:
        await transaction.send(b"first", "original")
        await transaction.send(b"remove", "q")
        journal = transaction.sent_frames
        journal[0].headers["destination"] = "changed"
        journal.pop()
        journal.append(
            stompman.SendFrame.build(
                body=b"inserted",
                destination="q",
                transaction=transaction.id,
                content_type=None,
                add_content_length=True,
                headers=None,
            )
        )
        await client.core.reconnect()
        assert broker.committed == []
        replay = broker.current.transactions[transaction.id]
        assert [frame.body for frame in replay] == [b"first", b"inserted"]
        assert replay[0].headers["destination"] == "changed"
        journal[0] = stompman.SendFrame.build(
            body=b"replacement",
            destination="q",
            transaction=transaction.id,
            content_type=None,
            add_content_length=True,
            headers=None,
        )
        await client.core.reconnect()
        assert [frame.body for frame in broker.current.transactions[transaction.id]] == [b"replacement", b"inserted"]
    assert [frame.body for frame in broker.committed[0]] == [b"replacement", b"inserted"]


async def test_legacy_triggering_send_is_excluded_from_replay_source(broker: ScriptedBroker) -> None:
    async with broker.client() as client, client.begin() as transaction:
        await transaction.send(b"accepted", "before")
        transaction.sent_frames[0].headers["destination"] = "after"
        first = broker.current
        broker.fail_before = lambda frame, connection: (
            connection is first and isinstance(frame, stompman.SendFrame) and frame.body == b"trigger"
        )
        await transaction.send(b"trigger", "q")
        assert broker.committed == []
    assert [frame.body for frame in broker.committed[0]] == [b"accepted", b"trigger"]
    assert broker.committed[0][0].headers["destination"] == "after"


async def test_replaced_delivered_frame_preserves_its_acknowledgement_capability(broker: ScriptedBroker) -> None:
    received: list[stompman.AckableMessageFrame] = []

    async def handle(frame: stompman.AckableMessageFrame) -> None:
        received.append(replace(frame, body=b"adapted"))

    async with broker.client() as client:
        subscription = await client.subscribe_with_manual_ack("q", handle)
        broker.current.deliver(subscription.id, b"original", ack_id="ack")
        await wait_until(lambda: bool(received))
        await received[0].ack()
        await received[0].nack()
        await wait_until(lambda: client.core.status.pending_messages == 0)
        settlements = [
            frame for frame in broker.current.writes if isinstance(frame, (stompman.AckFrame, stompman.NackFrame))
        ]
        assert len(settlements) == 1
        assert isinstance(settlements[0], stompman.AckFrame)
        await subscription.unsubscribe()


async def test_maybe_write_reports_a_connection_lost_while_waiting_for_the_writer(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    original_write = broker.connection_class.write_frame

    async def write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
        if isinstance(frame, stompman.SendFrame):
            started.set()
            await release.wait()
            raise stompman.ConnectionLostError(reason="write failed")
        await original_write(connection, frame)

    monkeypatch.setattr(broker.connection_class, "write_frame", write)
    async with broker.client(write_retry_attempts=1) as client:
        sending = asyncio.create_task(client.send(b"body", "q"))
        await started.wait()
        maybe = asyncio.create_task(
            client._connection_manager.maybe_write_frame(stompman.AckFrame(headers={"id": "ack", "subscription": "s"}))
        )
        await asyncio.sleep(0)
        release.set()
        with pytest.raises(stompman.FailedAllWriteAttemptsError):
            await sending
        assert await maybe is False
