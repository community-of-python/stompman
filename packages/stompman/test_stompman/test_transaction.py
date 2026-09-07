import asyncio

import pytest
import stompman
from stompman.core import Confirmed, TransactionState, Unconfirmed

from test_stompman.conftest import ScriptedBroker, ScriptedConnection, wait_until

pytestmark = pytest.mark.anyio


async def test_send_and_commit_keep_headers_independent(broker: ScriptedBroker) -> None:
    headers = {"expires": "123"}
    async with broker.client() as client:
        async with client.begin() as transaction:
            await transaction.send(b"one", "first", content_type="text/plain", headers=headers)
            await transaction.send(b"longer", "second", headers=headers, add_content_length=False)
            await client.send(b"outside", "third", headers=headers)
        assert type(transaction) is stompman.Transaction
    assert headers == {"expires": "123"}
    first, second = broker.committed[0]
    assert first.headers == {
        "transaction": transaction.id,
        "destination": "first",
        "content-length": "3",
        "content-type": "text/plain",
        "expires": "123",
    }
    assert second.headers == {"transaction": transaction.id, "destination": "second", "expires": "123"}
    outside = next(
        frame for frame in broker.current.writes if isinstance(frame, stompman.SendFrame) and frame.body == b"outside"
    )
    assert "transaction" not in outside.headers


async def test_transaction_aborts_on_body_error(broker: ScriptedBroker) -> None:
    async with broker.client() as client:
        with pytest.raises(ValueError, match="abort"):
            async with client.begin() as transaction:
                await transaction.send(b"no", "q")
                msg = "abort"
                raise ValueError(msg)
        assert any(isinstance(frame, stompman.AbortFrame) for frame in broker.current.writes)
    assert broker.committed == []


async def test_open_transaction_is_restored_without_early_commit(broker: ScriptedBroker) -> None:
    async with broker.runtime() as runtime:
        async with runtime.begin(transaction_id="tx") as transaction:
            await transaction.send(b"first", "q")
            await runtime.reconnect()
            assert transaction.state.value == TransactionState.OPEN.value
            assert broker.committed == []
            assert [type(frame) for frame in broker.current.writes] == [
                stompman.ConnectFrame,
                stompman.BeginFrame,
                stompman.SendFrame,
            ]
            await transaction.send(b"second", "q")
        assert transaction.state == TransactionState.COMPLETED
    assert [frame.body for frame in broker.committed[0]] == [b"first", b"second"]


async def test_failed_triggering_send_is_not_replayed_twice(broker: ScriptedBroker) -> None:
    async with broker.runtime() as runtime:
        first_connection = broker.current
        async with runtime.begin(confirmation=Unconfirmed(attempts=3)) as transaction:
            await transaction.send(b"first", "q")
            broker.fail_before = lambda frame, connection: (
                connection is first_connection and isinstance(frame, stompman.SendFrame) and frame.body == b"second"
            )
            await transaction.send(b"second", "q")
        assert first_connection.closed
    assert [frame.body for frame in broker.committed[0]] == [b"first", b"second"]


async def test_replay_journal_cannot_be_mutated_by_caller(broker: ScriptedBroker) -> None:
    async with broker.runtime() as runtime, runtime.begin() as transaction:
        await transaction.send(b"first", "q")
        frames = transaction.sent_frames
        frames[0].headers["destination"] = "wrong"
        frames.clear()
        await runtime.reconnect()
    assert broker.committed[0][0].headers["destination"] == "q"


async def test_unknown_commit_is_reported_and_never_replayed(broker: ScriptedBroker) -> None:
    async with broker.runtime() as runtime:
        first = broker.current
        broker.fail_after = lambda frame, connection: connection is first and isinstance(frame, stompman.CommitFrame)
        with pytest.raises(stompman.TransactionOutcomeUnknownError):
            async with runtime.begin() as transaction:
                await transaction.send(b"once", "q")
        assert transaction.state is TransactionState.UNCERTAIN
        await runtime.reconnect()
        assert not any(isinstance(frame, stompman.BeginFrame) for frame in broker.current.writes)
    assert len(broker.committed) == 1


async def test_commit_receipt_timeout_is_unknown(broker: ScriptedBroker) -> None:
    broker.receipts = False
    async with broker.runtime() as runtime:
        with pytest.raises(stompman.TransactionOutcomeUnknownError) as info:
            async with runtime.begin(confirmation=Unconfirmed(), commit_confirmation=Confirmed(0.05)) as transaction:
                await transaction.send(b"accepted", "q")
        assert isinstance(info.value.reason, stompman.ReceiptTimeoutError)
        assert transaction.state is TransactionState.UNCERTAIN
        await runtime.reconnect()
    assert len(broker.committed) == 1


async def test_receipt_confirmed_transaction_and_completed_reuse_rejected(broker: ScriptedBroker) -> None:
    async with broker.runtime() as runtime:
        async with runtime.begin(commit_confirmation=Confirmed(0.1)) as transaction:
            await transaction.send(b"one", "q")
        with pytest.raises(RuntimeError, match="completed"):
            await transaction.send(b"two", "q")
        with pytest.raises(RuntimeError, match="already"):
            await transaction.__aenter__()


async def test_abort_after_connection_loss_does_not_reconnect(broker: ScriptedBroker) -> None:
    async with broker.runtime() as runtime:
        transaction = await runtime.begin().__aenter__()
        await transaction.send(b"one", "q")
        # The replacement has a valid BEGIN; abort remains legal after restoration.
        await runtime.reconnect()
        await transaction.abort()
        assert transaction.state is TransactionState.ABORTED
    assert broker.committed == []


@pytest.mark.parametrize("reject_before_drain", [True, False])
@pytest.mark.parametrize("accepted_body", [b"accepted", b"rejected"])
async def test_rejected_send_is_removed_before_transaction_restoration(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch, accepted_body: bytes, *, reject_before_drain: bool
) -> None:
    original_write = broker.connection_class.write_frame
    rejections: asyncio.Queue[tuple[ScriptedConnection, stompman.ErrorFrame]] = asyncio.Queue()
    publications = 0

    async def write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
        nonlocal publications
        if isinstance(frame, stompman.SendFrame) and "receipt" in frame.headers:
            publications += 1
        if isinstance(frame, stompman.SendFrame) and publications == 2 and "receipt" in frame.headers:
            rejection = stompman.ErrorFrame(headers={"receipt-id": frame.headers["receipt"], "message": "rejected"})
            if reject_before_drain:
                connection.incoming.put_nowait(rejection)
                await asyncio.sleep(0)
            else:
                rejections.put_nowait((connection, rejection))
            return
        await original_write(connection, frame)

    monkeypatch.setattr(broker.connection_class, "write_frame", write)
    async with broker.runtime() as runtime, runtime.begin(transaction_id="tx") as transaction:
        await transaction.send(accepted_body, "q")
        send = asyncio.create_task(transaction.send(b"rejected", "q"))
        if not reject_before_drain:
            connection, rejection = await rejections.get()
            await wait_until(lambda: not runtime.status.writing)
            connection.incoming.put_nowait(rejection)
        with pytest.raises(stompman.ReceiptRejectedError):
            await send
        await runtime.reconnect()
        assert [frame.body for frame in transaction.sent_frames] == [accepted_body]
        assert [frame.body for frame in broker.current.transactions["tx"]] == [accepted_body]
    assert [frame.body for frame in broker.committed[0]] == [accepted_body]


async def test_external_replay_journal_is_snapshotted_before_begin(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch
) -> None:
    source: list[stompman.SendFrame] = []
    entered = asyncio.Event()
    release = asyncio.Event()
    original_write = broker.connection_class.write_frame

    async def write(connection: ScriptedConnection, frame: stompman.AnyClientFrame) -> None:
        if isinstance(frame, stompman.BeginFrame):
            entered.set()
            await release.wait()
        await original_write(connection, frame)

    async with broker.runtime() as runtime, runtime.begin(replay_source=lambda: tuple(source)) as transaction:
        await transaction.send(b"first", "original")
        source[:] = transaction.sent_frames
        monkeypatch.setattr(broker.connection_class, "write_frame", write)
        reconnect = asyncio.create_task(runtime.reconnect())
        try:
            await entered.wait()
            source[0].headers["destination"] = "mutated"
            source.clear()
        finally:
            release.set()
            await reconnect
        replayed = broker.current.transactions[transaction.id]
        assert len(replayed) == 1
        assert replayed[0].headers["destination"] == "original"
    assert broker.committed[0][0].headers["destination"] == "original"
