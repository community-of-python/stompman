from typing import Any

import pytest
from stompman import (
    SendFrame,
)
from stompman.frames import SendHeaders

from test_stompman.conftest import ScriptedBroker

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize(
    ("args", "expected_body", "expected_headers"),
    [
        (
            {"body": b"Some body", "destination": "Some/queue"},
            b"Some body",
            {"content-length": "9", "destination": "Some/queue"},
        ),
        (
            {"body": b"Some body", "destination": "Some/queue", "add_content_length": True},
            b"Some body",
            {"content-length": "9", "destination": "Some/queue"},
        ),
        (
            {"body": b"Some body", "destination": "Some/queue", "content_type": "text/plain"},
            b"Some body",
            {"content-length": "9", "destination": "Some/queue", "content-type": "text/plain"},
        ),
        (
            {"body": b"Some body", "destination": "Some/queue", "add_content_length": False},
            b"Some body",
            {"destination": "Some/queue"},
        ),
    ],
)
async def test_send_message(
    broker: ScriptedBroker, args: dict[str, Any], expected_body: bytes, expected_headers: SendHeaders
) -> None:
    async with broker.client() as client:
        await client.send(**args)
    assert broker.current.writes[1] == SendFrame(headers=expected_headers, body=expected_body)
