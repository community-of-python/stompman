# Session core and adapters

`stompman.core` is a self-contained STOMP library. It imports its own modules and
the standard library. Importing it does not load the legacy facade. Frames, codec,
configuration, errors, TCP transport, session execution, and recovery all live in
core. The legacy `stompman.frames` and `stompman.serde` paths re-export the
canonical protocol definitions, preserving class identity. Legacy error names
remain available; diagnostic adapters retain the original connection-parameter
payloads where the native configuration uses different types.

FastStream is the primary facade and executes core directly when configured with
servers, `RuntimeConfig`, or `Runtime`. `Client` is a separate compatibility
adapter. Explicitly supplying a Client to StompBroker preserves that exact object,
its execution overrides, lifecycle hooks, and historical write-only defaults.

```mermaid
flowchart TD
    FastStream[FastStream facade] --> Native[Native adapter]
    Native --> Runtime
    FastStream --> Injected[Injected Client adapter]
    Injected --> Client
    Client[Client compatibility adapter] --> Runtime
    Client --> Compatibility[Legacy configuration and transport bridge]
    Runtime --> Recovery[ConnectionSupervisor]
    Runtime --> Subscriptions
    Runtime --> Deliveries
    Runtime --> Transaction
    Subscriptions --> Recovery
    Transaction --> Recovery
    Subscriptions --> Deliveries
    Recovery --> Session
    Recovery --> Connector
    Connector --> Handshake[NegotiatedConnection]
    Deliveries --> Session
    Session --> Transport[Transport interface]
    Transport --> TCP[Native TCP]
    Transport --> Compatibility
    Session --> Commands
    Commands --> Receipts
    Deliveries --> Capacity
    Deliveries --> Settlement
    TCP --> Codec[Core frames and codec]
```

## Ownership and review map

| Module | Interface | Invariants it owns |
| --- | --- | --- |
| `Runtime` | Start, close, send, subscribe, begin, status | Complete running resources; draining handlers can still publish and settle |
| `Connector` / `NegotiatedConnection` | Connect, close | Handshake success before construction; every losing connection is closed |
| `ConnectionSupervisor` | Run a submission, reconnect, attach/detach restoration intent | Retry spacing, generation changes, consistent journal restoration |
| `Session` | Create a command, write, close | One negotiated transport and reader, write order, heartbeats, deterministic cleanup |
| `Commands` / `Command` | Submit, complete, cancel | Owned execution and receipt lifetime; callers never need a separate command cleanup step |
| `Receipts` | Reserve, receive, discard, fail | Exact receipt correlation; retire correlations before rejection observers |
| `FrameParser` | Parse a chunk | Command, headers, and body are distinct phases; completed frames own their headers |
| `Subscriptions` / `Subscription` | Subscribe, receive, restore, unsubscribe | Immutable intent, explicit waiting/installing/active/removing/removed states, callback ordering, safe ID reuse |
| `Deliveries` / `Channel` | Admit, pause, drain | Handler scheduling and channel lifetime |
| `Capacity` / `Reservation` | Reserve, finish handler, finish settlement | Admission stays charged until both owners finish exactly once |
| `ManualAcknowledgements` | Register, settle, finish | Ordered ACK/NACK on the original session; queue membership means unsettled |
| `Transaction` | Enter, send, commit, abort | Immutable journal entries, typed open/committing/finished states, ambiguous commit handling |

Start reading with `core/runtime.py`: it composes the owners and describes the
application lifecycle. Follow a publication through `recovery.py` and `command.py`;
the command owns the asynchronous work and exposes just submission and completion.
Then read `subscriptions.py` for installation and recovery, `delivery.py` for handler
execution, and `transaction.py` for the journal. `session.py`, `handshake.py`, and
`connector.py` contain the transport lifecycle. Capacity and settlement details
stay in their own modules and do not spread into facade methods.

Transactions and subscriptions implement the same small restoration interface.
They never inspect Runtime fields. Delivery settlement holds a capability tied to
its original channel and session; it cannot obtain a replacement session.

The generation gate covers session acquisition, restoration, transport submission,
and the corresponding journal update. Normal receipt waits happen after releasing
the gate, allowing unrelated commands to finish in any receipt order. Restoring a
session waits for subscription confirmations before allowing new submissions.
A command succeeds only after both transport drain and its exact receipt succeed;
a receipt arriving before a stalled drain cannot hide a write timeout.

A capacity reservation gives the handler and settlement separate idempotent
completion capabilities. Capacity is released when both finish. Queued deliveries, running handlers, and
completed handlers with unsettled messages all count toward admission. Channels
serialize their settlement ledger and retire old generations without redirecting
ACK/NACK. Unsubscribe waits for already requested settlements before its wire
command and reserves its subscription ID until removal finishes. Concurrent
unsubscribe calls join the same cleanup task. A failed removal confirmation
retires the original session before that ID can be reused. Graceful shutdown
first pauses admission and drains running handlers.

If a cumulative ACK/NACK sequence is interrupted, its original session is retired.
This allows the broker to redeliver later messages whose decisions were already
queued, without replaying an acknowledgement with an unknown outcome.

The incremental parser retains one line or body buffer. Header and body phases
require a recognized command, so there are no partially initialized combinations
of command and parsing flags. Both LF and CRLF heartbeats survive chunk boundaries.
Malformed content lengths use the existing delimiter-based fallback, including
negative lengths, so they cannot consume all following frames.

An open transaction appends immutable entries under the generation gate; finished
transactions retain a frozen snapshot. A rejected SEND removes its own entry in
the reader before any recovery can replay it. A failed unconfirmed resubscription
returns to waiting for a session, preserving intent for the next connection attempt.

Cancellation before COMMIT submission leaves the transaction open. An ABORT
request immediately withdraws replay intent and owns its cleanup; cancellation is
propagated after that cleanup completes. This prevents a transaction being replayed
while its caller is already trying to abort it.

## Native interface

Native configuration is frozen and validated at construction. Server collections
are tuples; CONNECT headers are immutable snapshots. Connection settings,
recovery policy, and delivery limits are distinct values. Credentials and endpoint
fields are required. Passwords passed to native `Server` are literal, while the
legacy adapter preserves URL decoding of `ConnectionParameters.passcode`.

```python
from stompman.core import (
    Confirmed,
    Delivery,
    DeliveryLimits,
    Runtime,
    RuntimeConfig,
    Server,
)

config = RuntimeConfig(
    servers=(Server("localhost", 61616, "guest", "guest"),),
    delivery=DeliveryLimits(concurrency=32, pending_messages=512),
)


async def handle(message: Delivery) -> None:
    print(message.body)
    await message.ack()


async with Runtime(config) as runtime:
    subscription = await runtime.subscribe("events", handle)
    receipt = await runtime.send(b"hello", "events")
    await runtime.send(b"custom deadline", "events", confirmation=Confirmed(10))
    async with runtime.begin() as transaction:
        await transaction.send(b"one", "events")
        await transaction.send(b"two", "events")
    await subscription.unsubscribe()
```

SEND, SUBSCRIBE, ACK/NACK, UNSUBSCRIBE, and transaction operations wait for broker
receipts by default, with a five-second operation deadline. DISCONNECT has its
own connection setting. Confirmation proves broker acceptance, not consumer
processing. A timeout includes transport submission and receipt waiting after a
connection becomes available. Confirmed publication never retries an ambiguous
write automatically.

`Unconfirmed(attempts=3)` explicitly selects write-only delivery with bounded
retries and possible duplicates. The default `Unconfirmed()` makes one attempt.
A subscription's `confirmation` controls installation and restoration;
`operation_confirmation` controls its settlement and removal. A transaction's
`confirmation` controls BEGIN, SEND, and ABORT; `commit_confirmation` controls
COMMIT. All of these default to `Confirmed()`.

An open transaction is restored with BEGIN and a private immutable send journal.
These reconstruction writes remain inside the transaction and use write-only
submission; the eventual confirmed COMMIT determines acceptance. The triggering
SEND enters the journal once, after its transport submission succeeds. COMMIT is
removed from restoration before it reaches the wire. Missing confirmation or
connection loss during COMMIT raises `TransactionOutcomeUnknownError`; COMMIT is
never blindly replayed.

By default, reconnect attempts are bounded and a quiet subscription does not
cause idle reconnects. Heartbeats still detect a dead peer. `ConnectionSettings`
can enable an idle deadline and configure TLS, heartbeat tolerance, read size,
and connection deadlines. `RecoveryPolicy` controls retry count, spacing, and
whether background recovery keeps trying after exhausting one cycle.

`RuntimeStatus` exposes generation, health, pending message/byte counts, running
handlers, subscription IDs, outstanding receipts, and active transport writes.
Applications need not inspect internal session or subscription dictionaries.
During restoration, status remains `recovering` and `is_alive()` is false.
The generation advances only after restoration finishes; receipt and write
diagnostics still describe the session being restored.

## Compatibility and FastStream

Client retains its constructor fields, dataclass extension, method signatures,
mutable public objects, callback shapes, exception payloads, and context behavior.
Normal Client context exit waits for subscriptions
to be removed. `_compat.LegacyOptions` translates old flat options into native
configuration and explicitly chooses unconfirmed operation policies. The core
contains no legacy fallback detection.

Legacy subscription handlers and callbacks are consulted at execution time.
Destination, headers, IDs, and server lists produce fresh immutable core snapshots
for restoration. The original subscription, acknowledgement, and transaction
constructors remain available. `Client.begin()` remains a lazy async context manager
and decorator, and the legacy transaction retains its stable mutable `sent_frames`
list. Changes to that list affect recovery through an explicit replay supplier;
the currently submitting frame stays outside replay because its sender owns retry.
Raw `ConnectionManager` and `ConnectionLifespan` extension modules remain
available through adapters.

Native admission defaults to 1,024 messages and 64 MiB. Legacy admission remains
unlimited unless `max_pending_messages` or `max_pending_bytes` is supplied;
`max_concurrent_handlers=None` remains unlimited and zero pauses dispatch.
The adapter expresses these choices as `Unbounded()` and `Paused()` policies.
Native numeric limits and timeouts still require valid positive values.

Legacy expired deadlines still invoke custom transport hooks and retain retry
spacing. Connection failures preserve the original server objects and timeout
values, including failures nested in background exception groups. A custom
restoration hook that loses its connection invalidates that session and retries.

Both Client subscription methods retain `receipt_timeout` and
`on_subscription_error`. Receipt confirmation is opt-in on this facade only.
`SubscriptionError` retains the reasons rejected, timeout, connection_lost, and
unsubscribed; raw ERROR frames are excluded from its representation. Failed
intent and receipt correlation are removed before callbacks. Callback exceptions
are logged and suppressed. Confirmed subscriptions restore with fresh receipt IDs;
failed or still-unconfirmed installations do not replay. Legacy write-only
subscriptions retain their restoration behavior.

The legacy transport bridge preserves custom Connection classes, `connect()`
returning None on failure, wall-clock `last_read_time`, synchronous heartbeat
fallbacks, WebSocket paths, and TLS. Existing raw Connection, WebSocketConnection,
FrameParser, and frame users keep their interfaces. A custom native transport
implements `Transport` and is supplied through `Runtime(transport_factory=...)`.
Its factory returns a connected transport or raises; it never returns None.

FastStream accepts `StompBroker(RuntimeConfig(...))`, an owned Runtime, or
`StompBroker(servers=[ConnectionParameters(...)])`. Native configuration can be
paired with an explicit `transport_factory`. Existing `StompBroker(Client(...))`
uses the injected object's `send`, `begin`, subscription, health, and lifecycle
methods. Existing subclasses, proxies, and mocks therefore keep working. Use
`RuntimeConfig` or `Runtime` when selecting native receipt-confirmed defaults.

FastStream continues delivering the actual `stompman.AckableMessageFrame` class.
Publish command headers remain mutable for middleware. Router, subscriber
specification, publisher, reply metadata, content-length overrides, and
`TestStompBroker` contracts remain covered by their existing tests.

## Validation

Tests import core with every legacy module blocked and relocate it under a
different package name, then perform a confirmed TCP publication. Behavioral
regressions cover independent receipt waits, cancellation at each write phase,
restoration, stale settlement, capacity, cumulative ACK/NACK, replacement IDs,
transaction replay, and graceful draining. FastStream tests disable Client execution
methods for the native path and exercise the injected object's methods on the legacy
path. Both paths cover publication, handlers, replies, restart, and shutdown.
Differential probes and permanent compatibility regressions compare constructors,
mutation, typing, defaults, and exception payloads with the pre-core implementation.
Integration suites use both asyncio backends and both brokers.

Set `STOMPMAN_ARTEMIS_PORT` and `STOMPMAN_CLASSIC_PORT` before Docker Compose and
pytest when using isolated broker ports. Run `scripts/wait_for_stomp_brokers.py`
before the integration suite. Tests on different Python versions must run
sequentially against shared brokers.

Publish stompman 3.16.0 or newer before this faststream-stomp release. The adapter
requires that version for core. Its temporary AnyIO <4.15 bound remains necessary
until FastDepends imports `anyio.to_thread` explicitly; verify an installed-package
smoke test before removing it. The WebSocket extra supports websockets 14 or newer.
