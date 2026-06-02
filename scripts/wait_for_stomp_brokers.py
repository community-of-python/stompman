from __future__ import annotations

import socket
import sys
import time
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Broker:
    name: str
    host: str
    port: int
    login: str
    passcode: str


class BrokerProtocolError(Exception):
    """Raised when a broker accepts TCP but not STOMP."""


class BrokerWaitTimeoutError(Exception):
    """Raised when a broker is not ready before the deadline."""


BROKERS = (
    Broker(name="ActiveMQ Artemis", host="127.0.0.1", port=9000, login="admin", passcode=":=123"),
    Broker(name="ActiveMQ Classic", host="127.0.0.1", port=9001, login="admin", passcode=":=123"),
)
CONNECT_TIMEOUT_SECONDS = 2.0
READ_TIMEOUT_SECONDS = 2.0
OVERALL_TIMEOUT_SECONDS = 90.0
RETRY_INTERVAL_SECONDS = 1.0


def _build_connect_frame(broker: Broker) -> bytes:
    return (
        "CONNECT\n"
        "accept-version:1.2\n"
        f"host:{broker.host}\n"
        f"login:{broker.login}\n"
        f"passcode:{broker.passcode}\n"
        "heart-beat:0,0\n"
        "\n"
        "\0"
    ).encode()


def _read_stomp_frame(sock: socket.socket) -> bytes:
    response = b""
    while b"\0" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            break
        response += chunk
    return response


def _probe_broker(broker: Broker) -> None:
    with socket.create_connection((broker.host, broker.port), timeout=CONNECT_TIMEOUT_SECONDS) as sock:
        sock.settimeout(READ_TIMEOUT_SECONDS)
        sock.sendall(_build_connect_frame(broker))
        response = _read_stomp_frame(sock)

    if response.startswith(b"CONNECTED\n"):
        return

    decoded_response = response.decode(errors="replace")
    message = f"{broker.name} did not accept a STOMP connection: {decoded_response!r}"
    raise BrokerProtocolError(message)


def _wait_for_broker(broker: Broker, deadline: float) -> None:
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            _probe_broker(broker)
        except (BrokerProtocolError, OSError) as error:
            last_error = error
        else:
            sys.stdout.write(f"{broker.name} is ready on {broker.host}:{broker.port}\n")
            sys.stdout.flush()
            return
        time.sleep(RETRY_INTERVAL_SECONDS)

    message = f"Timed out waiting for {broker.name} on {broker.host}:{broker.port}: {last_error}"
    raise BrokerWaitTimeoutError(message)


def main() -> int:
    deadline = time.monotonic() + OVERALL_TIMEOUT_SECONDS
    for broker in BROKERS:
        _wait_for_broker(broker, deadline)
    return 0


if __name__ == "__main__":
    sys.exit(main())
