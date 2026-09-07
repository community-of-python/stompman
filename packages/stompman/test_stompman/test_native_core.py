"""Native guarantees, exercised through the same seams as application adapters."""

import asyncio
import dataclasses
import shutil
import subprocess  # ruff: ignore[suspicious-subprocess-import]
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
import stompman
import stompman.core
from stompman.core import (
    Confirmed,
    ConnectionSettings,
    Delivery,
    DeliveryLimits,
    RecoveryPolicy,
    RuntimeConfig,
    Server,
    Unconfirmed,
)

from test_stompman.conftest import ScriptedBroker, ScriptedConnection, wait_until


def test_core_import_does_not_load_legacy_facades() -> None:
    code = """
import sys
import stompman.core
legacy = {'stompman.client', 'stompman.config', 'stompman.connection', 'stompman.errors',
          'stompman.frames', 'stompman.serde', 'stompman.subscription', 'stompman.transaction', 'stompman.logger'}
assert not legacy.intersection(sys.modules), legacy.intersection(sys.modules)
"""
    subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True)  # ruff: ignore[subprocess-without-shell-equals-true]


def test_core_can_be_relocated_without_stompman(tmp_path: Path) -> None:
    shutil.copytree(Path(stompman.core.__file__).parent, tmp_path / "independent_core")
    code = """
import asyncio
import importlib.abc
import sys
class BlockLegacy(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path, target=None):
        if fullname == 'stompman' or fullname.startswith('stompman.'):
            raise AssertionError('core imported legacy code: ' + fullname)
sys.meta_path.insert(0, BlockLegacy())
sys.path.insert(0, sys.argv[1])
from independent_core import Runtime, RuntimeConfig, Server
from independent_core.frames import ConnectFrame, ConnectedFrame, SendFrame, ReceiptFrame
from independent_core.serde import FrameParser, dump_frame

async def broker(reader, writer):
    parser = FrameParser()
    try:
        while data := await reader.read(4096):
            for frame in parser.parse_frames_from_chunk(data):
                if isinstance(frame, ConnectFrame):
                    writer.write(dump_frame(ConnectedFrame(headers={'version': '1.2', 'heart-beat': '0,0'})))
                elif receipt := frame.headers.get('receipt'):
                    writer.write(dump_frame(ReceiptFrame(headers={'receipt-id': receipt})))
                await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()

async def main():
    server = await asyncio.start_server(broker, '127.0.0.1', 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        async with Runtime(RuntimeConfig((Server('127.0.0.1', port, 'guest', 'guest'),))) as runtime:
            receipt = await runtime.send(b'isolated', 'queue')
            assert isinstance(receipt, ReceiptFrame)
    assert not any(name == 'stompman' or name.startswith('stompman.') for name in sys.modules)
asyncio.run(main())
"""
    subprocess.run([sys.executable, "-I", "-c", code, str(tmp_path)], check=True, capture_output=True, text=True)  # ruff: ignore[subprocess-without-shell-equals-true]


def test_protocol_reexports_preserve_class_identity() -> None:
    from stompman.core import frames, serde  # ruff: ignore[import-outside-top-level]

    assert stompman.SendFrame is frames.SendFrame
    assert stompman.MessageFrame is frames.MessageFrame
    assert stompman.FrameParser is serde.FrameParser
    assert stompman.dump_frame is serde.dump_frame


@pytest.mark.parametrize("invalid", [0, -1, float("nan"), float("inf")])
def test_confirmation_rejects_invalid_timeouts(invalid: float) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        Confirmed(invalid)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: DeliveryLimits(concurrency=1.5),  # type: ignore[arg-type]
        lambda: DeliveryLimits(pending_messages=True),
        lambda: RecoveryPolicy(attempts=1.5),  # type: ignore[arg-type]
        lambda: Unconfirmed(attempts=False),
        lambda: ConnectionSettings(read_chunk_size=1.5),  # type: ignore[arg-type]
        lambda: Server("localhost", 1.5, "guest", "guest"),  # type: ignore[arg-type]
    ],
)
def test_configuration_rejects_invalid_integer_fields(factory: Callable[[], object]) -> None:
    with pytest.raises(ValueError, match=r"positive integer|port between"):
        factory()


def test_native_configuration_is_an_immutable_snapshot() -> None:
    headers = {"client-id": "original"}
    server = Server("localhost", 61616, "guest", "guest", headers)
    config = RuntimeConfig((server,))
    headers["client-id"] = "modified"
    assert server.connect_headers["client-id"] == "original"
    with pytest.raises(dataclasses.FrozenInstanceError):
        config.recovery = RecoveryPolicy(attempts=5)  # type: ignore[misc]
    with pytest.raises(TypeError):
        server.connect_headers["client-id"] = "modified"  # type: ignore[index]


@pytest.mark.anyio
async def test_native_operations_request_receipts_by_default(broker: ScriptedBroker) -> None:
    async def handle(delivery: Delivery) -> None:
        await delivery.ack()

    async with broker.runtime() as runtime:
        subscription = await runtime.subscribe("q", handle)
        receipt = await runtime.send(b"confirmed", "q")
        assert isinstance(receipt, stompman.ReceiptFrame)
        broker.current.deliver(subscription.id, b"one", ack_id="ack")
        await wait_until(
            lambda: (
                runtime.status.pending_messages == 0
                and any(isinstance(f, stompman.AckFrame) for f in broker.current.writes)
            )
        )
        async with runtime.begin() as transaction:
            await transaction.send(b"transactional", "q")
        await subscription.unsubscribe()
    for frame in broker.current.writes:
        if not isinstance(frame, stompman.ConnectFrame):
            assert frame.headers.get("receipt"), type(frame).__name__


@pytest.mark.anyio
async def test_receipt_wait_does_not_serialize_other_publications(broker: ScriptedBroker) -> None:
    async with broker.runtime() as runtime:
        broker.receipts = False
        first = asyncio.create_task(runtime.send(b"first", "q"))
        await wait_until(lambda: runtime.status.pending_receipts == 1 and not runtime.status.writing)
        second = asyncio.create_task(runtime.send(b"second", "q"))
        try:
            await wait_until(lambda: runtime.status.pending_receipts == 2 and not runtime.status.writing)
            sent = [frame for frame in broker.current.writes if isinstance(frame, stompman.SendFrame)]
            broker.current.incoming.put_nowait(
                stompman.ReceiptFrame(headers={"receipt-id": sent[1].headers["receipt"]})
            )
            assert isinstance(await second, stompman.ReceiptFrame)
            assert not first.done()
            broker.current.incoming.put_nowait(
                stompman.ReceiptFrame(headers={"receipt-id": sent[0].headers["receipt"]})
            )
            assert isinstance(await first, stompman.ReceiptFrame)
        finally:
            first.cancel()
            second.cancel()
            await asyncio.gather(first, second, return_exceptions=True)
            broker.receipts = True


@pytest.mark.anyio
async def test_native_uncertain_send_is_not_retried(broker: ScriptedBroker) -> None:
    async with broker.runtime() as runtime:
        broker.fail_after = lambda frame, connection: isinstance(frame, stompman.SendFrame)
        with pytest.raises(stompman.ConnectionLostError):
            await runtime.send(b"uncertain", "q")
        assert sum(isinstance(f, stompman.SendFrame) for c in broker.connections for f in c.writes) == 1
        broker.fail_after = None


@pytest.mark.anyio
async def test_unused_transaction_cannot_reopen_a_closed_runtime(broker: ScriptedBroker) -> None:
    async with broker.runtime() as runtime:
        transaction = runtime.begin()
    calls = broker.connect_calls
    with pytest.raises(RuntimeError, match="not running"):
        await transaction.__aenter__()
    assert broker.connect_calls == calls


@pytest.mark.anyio
async def test_duplicate_transaction_is_rejected_before_writing(broker: ScriptedBroker) -> None:
    async with broker.runtime() as runtime, runtime.begin(transaction_id="same"):
        before = len(broker.current.writes)
        with pytest.raises(ValueError, match="already active"):
            await runtime.begin(transaction_id="same").__aenter__()
        assert len(broker.current.writes) == before
        assert runtime.status.pending_receipts == 0


@pytest.mark.anyio
async def test_failed_subscription_cleanup_preserves_replacement_with_same_id(broker: ScriptedBroker) -> None:
    replacements: list[asyncio.Task[stompman.core.Subscription]] = []
    async with broker.runtime() as runtime:
        broker.receipts = False

        def replace_subscription(error: stompman.SubscriptionError) -> None:
            replacements.append(
                asyncio.create_task(runtime.subscribe("q", lambda _: asyncio.sleep(0), subscription_id="same"))
            )

        original = asyncio.create_task(
            runtime.subscribe(
                "q",
                lambda _: asyncio.sleep(0),
                subscription_id="same",
                on_subscription_error=replace_subscription,
            )
        )
        await wait_until(lambda: "same" in broker.current.subscriptions and not runtime.status.writing)
        frame = broker.current.subscriptions.pop("same")
        broker.receipts = True
        broker.current.incoming.put_nowait(
            stompman.ErrorFrame(headers={"message": "rejected", "receipt-id": frame.headers["receipt"]})
        )
        with pytest.raises(stompman.SubscriptionError, match="rejected"):
            await original
        await wait_until(lambda: bool(replacements))
        replacement = await replacements[0]
        assert runtime.status.subscription_ids == ("same",)
        assert "same" in broker.current.subscriptions
        await replacement.unsubscribe()


@pytest.mark.anyio
async def test_unsubscribe_waits_for_already_requested_settlements(
    broker: ScriptedBroker,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    seen: list[bytes] = []
    original_write = broker.connection_class.write_frame

    async def write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
        if isinstance(frame, stompman.AckFrame):
            entered.set()
            await release.wait()
        await original_write(connection, frame)

    monkeypatch.setattr(broker.connection_class, "write_frame", write)

    async def handle(delivery: Delivery) -> None:
        seen.append(delivery.body)
        await delivery.ack()

    async with broker.runtime() as runtime:
        subscription = await runtime.subscribe("q", handle)
        broker.current.deliver(subscription.id, b"one", ack_id="one")
        broker.current.deliver(subscription.id, b"two", ack_id="two")
        await entered.wait()
        await wait_until(lambda: len(seen) == 2)
        unsubscribe = asyncio.create_task(subscription.unsubscribe())
        await asyncio.sleep(0)
        assert not unsubscribe.done()
        release.set()
        await unsubscribe
        relevant = [
            type(frame)
            for frame in broker.current.writes
            if isinstance(frame, (stompman.AckFrame, stompman.UnsubscribeFrame))
        ]
        assert relevant == [stompman.AckFrame, stompman.AckFrame, stompman.UnsubscribeFrame]
        await wait_until(lambda: runtime.status.pending_messages == 0)


@pytest.mark.anyio
async def test_recovery_reports_ready_only_after_restoration_finishes(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    entering = asyncio.Event()
    release = asyncio.Event()
    original_write = broker.connection_class.write_frame
    deliveries: asyncio.Queue[Delivery] = asyncio.Queue()

    async with broker.runtime() as runtime:
        first = broker.current
        subscription = await runtime.subscribe("q", deliveries.put)

        async def write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
            if connection is not first and isinstance(frame, stompman.SubscribeFrame):
                entering.set()
                await release.wait()
            await original_write(connection, frame)

        monkeypatch.setattr(broker.connection_class, "write_frame", write)
        broker.receipts = False
        reconnect = asyncio.create_task(runtime.reconnect())
        try:
            await entering.wait()
            assert runtime.status.state == "recovering"
            assert runtime.status.generation == 1
            assert not runtime.is_alive()
            assert runtime.status.writing

            release.set()
            await wait_until(lambda: subscription.id in broker.current.subscriptions and not runtime.status.writing)
            assert runtime.status.state == "recovering"
            assert runtime.status.generation == 1
            assert not runtime.is_alive()
            assert runtime.status.pending_receipts == 1

            restored = broker.current.subscriptions[subscription.id]
            broker.current.incoming.put_nowait(
                stompman.ReceiptFrame(headers={"receipt-id": restored.headers["receipt"]})
            )
            await reconnect
            ready = runtime.status
            assert ready.state == "connected"
            assert ready.generation == 2
            assert runtime.is_alive()
            assert ready.pending_receipts == 0

            broker.receipts = True
            broker.current.deliver(subscription.id, b"restored", ack_id="restored")
            delivery = await deliveries.get()
            await delivery.ack()
        finally:
            release.set()
            broker.receipts = True
            reconnect.cancel()
            await asyncio.gather(reconnect, return_exceptions=True)


@pytest.mark.anyio
async def test_close_cancels_owned_reconnection_without_leaking_transport(broker: ScriptedBroker) -> None:
    from stompman.core import Runtime  # ruff: ignore[import-outside-top-level]
    from stompman.core.transport import Transport  # ruff: ignore[import-outside-top-level]

    entering = asyncio.Event()
    release = asyncio.Event()
    connect = broker.transport

    async def factory(server: Server, settings: ConnectionSettings) -> Transport:
        if broker.connections:
            entering.set()
            await release.wait()
        return await connect(server, settings)

    runtime = Runtime(broker.config(), transport_factory=factory)
    await runtime.start()
    reconnect = asyncio.create_task(runtime.reconnect())
    await entering.wait()
    await asyncio.wait_for(runtime.close(), 1)
    release.set()
    with pytest.raises(RuntimeError, match="closed"):
        await reconnect
    await runtime.close()
    assert runtime.status.state == "closed"
    assert all(connection.closed for connection in broker.connections)
    assert broker.connect_calls == 1


@pytest.mark.anyio
async def test_close_interrupts_unconfirmed_transport_write(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    entering = asyncio.Event()
    original_write = broker.connection_class.write_frame

    async def write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
        if isinstance(frame, stompman.SendFrame):
            entering.set()
            await asyncio.Future()
        await original_write(connection, frame)

    monkeypatch.setattr(broker.connection_class, "write_frame", write)
    runtime = broker.runtime()
    await runtime.start()
    sending = asyncio.create_task(runtime.send(b"blocked", "q", confirmation=Unconfirmed(attempts=3)))
    await entering.wait()
    await asyncio.wait_for(runtime.close(), 1)
    with pytest.raises(RuntimeError, match="not running"):
        await sending
    assert all(connection.closed for connection in broker.connections)


@pytest.mark.anyio
@pytest.mark.parametrize("operation", ["abort", "commit"])
async def test_cancelled_finalization_during_restore_has_a_defined_outcome(
    broker: ScriptedBroker,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    entering = asyncio.Event()
    release = asyncio.Event()
    original_write = broker.connection_class.write_frame
    async with broker.runtime() as runtime:
        first = broker.current
        transaction = await runtime.begin(transaction_id="tx").__aenter__()
        await transaction.send(b"kept", "q")

        async def write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
            if connection is not first and isinstance(frame, stompman.BeginFrame):
                entering.set()
                await release.wait()
            await original_write(connection, frame)

        monkeypatch.setattr(broker.connection_class, "write_frame", write)
        reconnect = asyncio.create_task(runtime.reconnect())
        await entering.wait()
        finalizing = asyncio.create_task(transaction.abort() if operation == "abort" else transaction.commit())
        await asyncio.sleep(0)
        finalizing.cancel()
        if operation == "abort":
            await asyncio.sleep(0)
            assert transaction.state == stompman.core.TransactionState.ABORTED
            assert not finalizing.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await finalizing
        await reconnect
        if operation == "commit":
            assert transaction.state == stompman.core.TransactionState.OPEN
            await transaction.abort()
        assert not broker.current.transactions
