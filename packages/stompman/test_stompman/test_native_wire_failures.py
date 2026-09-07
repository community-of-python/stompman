"""A socket peer supplies independent wire data and observes client closure."""

import asyncio

import pytest
from stompman.core import ConnectionSettings, FrameLimits, Heartbeat, RecoveryPolicy, Runtime, RuntimeConfig, Server
from stompman.core.errors import ProtocolError, ReceiptRejectedError

pytestmark = pytest.mark.anyio


@pytest.mark.parametrize("failure", ["error", "bad_escape", "wrong_direction", "oversize"])
async def test_native_wire_failure_closes_the_socket(failure: str) -> None:
    closed = asyncio.Event()
    bytes_after_failure: list[bytes] = []

    async def respond(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:  # ruff: ignore[too-many-statements-in-try-clause]
            connect = await reader.readuntil(b"\x00")
            assert connect.startswith(b"CONNECT\n")
            writer.write(b"CONNECTED\nversion:1.2\nheart-beat:0,0\n\n\x00")
            await writer.drain()
            while request := await reader.readuntil(b"\x00"):
                receipt = next(line.partition(b":")[2] for line in request.split(b"\n") if line.startswith(b"receipt:"))
                if request.startswith(b"SEND\n"):
                    wire = {
                        "error": b"ERROR\nreceipt-id:" + receipt + b"\n\n\x00",
                        "bad_escape": b"MESSAGE\nx:bad\\tvalue\n\nbody\x00",
                        "wrong_direction": b"SEND\ndestination:q\n\nbody\x00",
                        "oversize": b"MESSAGE\nx:" + b"a" * 129,
                    }[failure]
                    writer.write(wire)
                    await writer.drain()
                    bytes_after_failure.append(await reader.read())
                    closed.set()
                    return
                writer.write(b"RECEIPT\nreceipt-id:" + receipt + b"\n\n\x00")
                await writer.drain()
        except asyncio.IncompleteReadError:
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(respond, "127.0.0.1", 0)
    async with server:
        port = server.sockets[0].getsockname()[1]
        runtime = Runtime(
            RuntimeConfig(
                (Server("127.0.0.1", port, "guest", "guest"),),
                connection=ConnectionSettings(heartbeat=Heartbeat(0, 0), frame_limits=FrameLimits(line_bytes=128)),
                recovery=RecoveryPolicy(attempts=1, delay=0),
            )
        )
        if failure == "error":
            async with runtime:
                with pytest.raises(ReceiptRejectedError):
                    await runtime.send(b"request", "q")
                await asyncio.wait_for(closed.wait(), 1)
        else:
            outcomes: list[object] = []
            with pytest.raises(ExceptionGroup) as error:
                async with runtime:
                    sending = asyncio.create_task(runtime.send(b"request", "q"))
                    try:
                        await asyncio.Future()
                    finally:
                        outcomes.extend(await asyncio.gather(sending, return_exceptions=True))
            assert any(isinstance(child, ProtocolError) for child in error.value.exceptions)
            assert len(outcomes) == 1
            await asyncio.wait_for(closed.wait(), 1)
        assert bytes_after_failure == [b""]
        assert runtime.status.state == "closed"
