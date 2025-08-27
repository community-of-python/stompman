import asyncio
from contextlib import suppress
from typing import TYPE_CHECKING
from unittest import mock

import faker
import pytest
import stompman.transaction
from stompman import (
    AbortFrame,
    BeginFrame,
    CommitFrame,
    SendFrame,
)
from stompman.errors import ConnectionLostError

from test_stompman.conftest import (
    CONNECT_FRAME,
    CONNECTED_FRAME,
    EnrichedClient,
    SomeError,
    create_spying_connection,
    enrich_expected_frames,
    get_read_frames_with_lifespan,
)

if TYPE_CHECKING:
    from stompman.frames import SendHeaders

pytestmark = pytest.mark.anyio


async def test_send_message_and_enter_transaction_ok(monkeypatch: pytest.MonkeyPatch, faker: faker.Faker) -> None:
    body, destination, expires, content_type = faker.binary(length=10), faker.pystr(), faker.pystr(), faker.pystr()

    transaction_id = faker.pystr()
    monkeypatch.setattr(stompman.transaction, "_make_transaction_id", mock.Mock(return_value=transaction_id))

    connection_class, collected_frames = create_spying_connection(*get_read_frames_with_lifespan([]))

    async with EnrichedClient(connection_class=connection_class) as client, client.begin() as transaction:
        await transaction.send(
            body=body, destination=destination, content_type=content_type, headers={"expires": expires}
        )
        await client.send(body=body, destination=destination, content_type=content_type, headers={"expires": expires})
        await asyncio.sleep(0)

    send_headers: SendHeaders = {  # type: ignore[typeddict-unknown-key]
        "content-length": str(len(body)),
        "content-type": content_type,
        "destination": destination,
        "expires": expires,
    }
    assert collected_frames == enrich_expected_frames(
        BeginFrame(headers={"transaction": transaction_id}),
        SendFrame(headers=send_headers | {"transaction": transaction_id}, body=body),
        SendFrame(headers=send_headers, body=body),
        CommitFrame(headers={"transaction": transaction_id}),
    )


async def test_send_message_and_enter_transaction_abort(monkeypatch: pytest.MonkeyPatch, faker: faker.Faker) -> None:
    transaction_id = faker.pystr()
    monkeypatch.setattr(stompman.transaction, "_make_transaction_id", mock.Mock(return_value=transaction_id))
    connection_class, collected_frames = create_spying_connection(*get_read_frames_with_lifespan([]))

    async with EnrichedClient(connection_class=connection_class) as client:
        with suppress(SomeError):
            async with client.begin():
                raise SomeError
        await asyncio.sleep(0)

    assert collected_frames == enrich_expected_frames(
        BeginFrame(headers={"transaction": transaction_id}), AbortFrame(headers={"transaction": transaction_id})
    )


async def test_commit_pending_transactions(monkeypatch: pytest.MonkeyPatch, faker: faker.Faker) -> None:
    body, destination = faker.binary(length=10), faker.pystr()
    monkeypatch.setattr(
        stompman.transaction,
        "_make_transaction_id",
        mock.Mock(side_effect=[(first_id := faker.pystr()), (second_id := faker.pystr())]),
    )
    connection_class, collected_frames = create_spying_connection(*get_read_frames_with_lifespan([CONNECTED_FRAME], []))
    async with EnrichedClient(connection_class=connection_class) as client:
        async with client.begin() as first_transaction:
            await first_transaction.send(body, destination=destination)
            client._connection_manager._clear_active_connection_state(ConnectionLostError(reason=""))
        async with client.begin() as second_transaction:
            await second_transaction.send(body, destination=destination)
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert collected_frames == enrich_expected_frames(
        BeginFrame(headers={"transaction": first_id}),
        SendFrame(
            headers={"destination": destination, "transaction": first_id, "content-length": str(len(body))}, body=body
        ),
        CONNECT_FRAME,
        CONNECTED_FRAME,
        SendFrame(
            headers={"destination": destination, "transaction": first_id, "content-length": str(len(body))}, body=body
        ),
        CommitFrame(headers={"transaction": first_id}),
        BeginFrame(headers={"transaction": second_id}),
        SendFrame(
            headers={"destination": destination, "transaction": second_id, "content-length": str(len(body))}, body=body
        ),
        CommitFrame(headers={"transaction": second_id}),
    )


def test_make_transaction_id() -> None:
    stompman.transaction._make_transaction_id()
