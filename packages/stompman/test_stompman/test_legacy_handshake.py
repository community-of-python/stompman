"""Native diagnostics must not change the legacy facade's diagnostic contract."""

import asyncio
from collections.abc import AsyncGenerator

import pytest
import stompman

from test_stompman.conftest import ScriptedBroker, ScriptedConnection

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("response", ["timeout", "eof", "error", "unsupported"])
async def test_legacy_handshake_preserves_collected_frames_and_public_issue_types(
    broker: ScriptedBroker, monkeypatch: pytest.MonkeyPatch, response: str
) -> None:
    prelude: list[stompman.MessageFrame | stompman.HeartbeatFrame | stompman.ReceiptFrame | stompman.ErrorFrame] = [
        stompman.HeartbeatFrame(),
        stompman.ReceiptFrame(headers={"receipt-id": "before-connected"}),
        stompman.ErrorFrame(headers={"message": "denied"}),
    ]

    async def read_frames(connection: ScriptedConnection) -> AsyncGenerator[stompman.AnyServerFrame, None]:
        if response == "timeout":
            await asyncio.Future()
        if response == "error":
            for frame in prelude:
                yield frame
        if response == "unsupported":
            yield stompman.ConnectedFrame(headers={"version": "1.1"})

    monkeypatch.setattr(broker.connection_class, "read_frames", read_frames)
    client = broker.client(connect_retry_attempts=1, connection_confirmation_timeout=0.01)
    with pytest.raises(stompman.FailedAllConnectAttemptsError) as failure:
        await client.__aenter__()
    expected = (
        stompman.UnsupportedProtocolVersion(given_version="1.1", supported_version="1.2")
        if response == "unsupported"
        else stompman.ConnectionConfirmationTimeout(timeout=0.01, frames=prelude if response == "error" else [])
    )
    assert failure.value.issues == [expected]
    assert broker.current.closed
