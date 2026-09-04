# stompman

A Python client for STOMP asynchronous messaging protocol that is:

- asynchronous,
- not abandoned,
- has typed, modern, comprehensible API.

## How To Use

Before you start using stompman, make sure you have it installed. If you optionally want to use
stompman over a websocket, you can install with `stompman[ws]` instead of `stompman`:

```sh
uv add stompman
poetry add stompman
```

Initialize a client:

```python
async with stompman.Client(
    servers=[
        stompman.ConnectionParameters(host="171.0.0.1", port=61616, login="user1", passcode="passcode1"),
        stompman.ConnectionParameters(host="172.0.0.1", port=61616, login="user2", passcode="passcode2"),
    ],
    # SSL — can be either `None` (default), `True`, or `ssl.SSLContext'
    ssl=None,
    # Error frame handler:
    on_error_frame=lambda error_frame: print(error_frame.body),
    # Optional parameters with sensible defaults:
    heartbeat=stompman.Heartbeat(will_send_interval_ms=1000, want_to_receive_interval_ms=1000),
    connect_retry_attempts=3,
    connect_retry_interval=1,
    connect_timeout=2,
    connection_confirmation_timeout=2,
    disconnect_confirmation_timeout=2,
    write_retry_attempts=3,
    check_server_alive_interval_factor=3,
    no_message_restart_interval=datetime.timedelta(hours=1),  # None to disable
    keep_alive_on_connection_failure=False,
) as client:
    ...
```

Initialize a client with a custom connection class, for example, connecting to a stomp producer
over websocket:

```python
# uv/poetry add stompman[ws] to get WebScoketConnection support
from stompman.connection_ws import WebSocketConnection

async with stompman.Client(
    servers=[
        stompman.ConnectionParameters(host="171.0.0.1", port=8080, login="", passcode="", ws_uri_path="/ws/path"),
    ],
    connection_class=WebSocketConnection,
    ...
) as client:
    ...
```

### Sending Messages

To send a message, use the following code:

```python
await client.send(b"hi there!", destination="DLQ", headers={"persistent": "true"})
```

Or, to send messages in a transaction:

```python
async with client.begin() as transaction:
    for _ in range(10):
        await transaction.send(body=b"hi there!", destination="DLQ", headers={"persistent": "true"})
        await asyncio.sleep(0.1)
```

### Listening for Messages

Now, let's subscribe to a destination and listen for messages:

```python
async def handle_message_from_dlq(message_frame: stompman.MessageFrame) -> None:
    print(message_frame.body)


await client.subscribe("DLQ", handle_message_from_dlq, on_suppressed_exception=print)
```

Entered `stompman.Client` will block forever waiting for messages if there are any active subscriptions.

Sometimes it's useful to avoid that:

```python
dlq_subscription = await client.subscribe("DLQ", handle_message_from_dlq, on_suppressed_exception=print)
await dlq_subscription.unsubscribe()
```

By default, subscription have ACK mode "client-individual". If handler successfully processes the message, an `ACK` frame will be sent. If handler raises an exception, a `NACK` frame will be sent. You can catch (and log) exceptions using `on_suppressed_exception` parameter:

```python
await client.subscribe(
    "DLQ",
    handle_message_from_dlq,
    on_suppressed_exception=lambda exception, message_frame: print(exception, message_frame),
)
```

You can change the ack mode used by specifying the `ack` parameter:

```python
# Server will assume that all messages sent to the subscription before the ACK'ed message are received and processed:
await client.subscribe("DLQ", handle_message_from_dlq, ack="client", on_suppressed_exception=print)

# Server will assume that messages are received as soon as it send them to client:
await client.subscribe("DLQ", handle_message_from_dlq, ack="auto", on_suppressed_exception=print)
```

You can pass custom headers to `client.subscribe()`:

```python
await client.subscribe(
    "DLQ",
    handle_message_from_dlq,
    ack="client",
    headers={"selector": "location = 'Europe'"},
    on_suppressed_exception=print,
)
```

#### Handling ACK/NACKs yourself

If you want to send ACK and NACK frames yourself, you can use `client.subscribe_with_manual_ack()`:

```python
async def handle_message_from_dlq(message_frame: stompman.AckableMessageFrame) -> None:
    print(message_frame.body)
    await message_frame.ack()


await client.subscribe_with_manual_ack("DLQ", handle_message_from_dlq, ack="client")
```

Note that this way exceptions won't be suppressed automatically.

#### Confirming subscriptions

Pass `receipt_timeout` to either subscription method to wait for the broker to
accept the subscription before returning. This uses standard
[STOMP receipts](https://stomp.github.io/stomp-specification-1.2.html#RECEIPT),
not broker-specific error messages.

```python
subscription = await client.subscribe_with_manual_ack(
    "DLQ",
    handle_message_from_dlq,
    receipt_timeout=3.0,
    on_subscription_error=lambda error: print(error.reason),
)
# It is now safe to publish a request that requires this response subscription.
```

The default `receipt_timeout=None` preserves the existing write-only behavior.
A timeout must be finite and positive. It covers writing `SUBSCRIBE` and waiting
for its receipt, after a connection is available. The client generates its own
`receipt` header in this mode.

An initial failure raises `SubscriptionError`, with `reason` equal to
`rejected`, `timeout`, `connection_lost`, or `unsubscribed`. The optional,
synchronous `on_subscription_error` callback also reports failures during
automatic resubscription, when there is no caller awaiting `subscribe()`.
The rejected subscription is removed before the callback runs. Callbacks should
not block; their exceptions are logged without terminating the frame reader.
Raw broker error frames are available through `error.frame`, but are excluded
from the exception's representation.

Confirmed subscriptions are restored after reconnect with fresh receipt IDs.
Unconfirmed or rejected subscriptions are not blindly replayed. Timeouts and
cancellation remove local state and attempt bounded cleanup on the same
connection. An `ERROR` without `receipt-id` fails all pending confirmations on
that connection; it does not remove previously confirmed subscriptions.
Neither subscription confirmation nor a publish receipt proves downstream
business processing.

The handler concurrency limit remains in effect while confirmations are
pending. Bounded delivery admission keeps the reader available for interleaved
receipts and errors. Configure broker prefetch/consumer-window settings together
with `max_pending_messages` and `max_pending_bytes`.

### Cleaning Up

stompman takes care of cleaning up resources automatically. When you leave the context of async context managers `stompman.Client()`, or `client.begin()`, the necessary frames will be sent to the server.

### Handling Connectivity Issues

- If multiple servers were provided, stompman will attempt to connect to each one simultaneously and will use the first that succeeds. If all servers fail to connect, an `stompman.FailedAllConnectAttemptsError` will be raised. In normal situation it doesn't need to be handled: tune retry and timeout parameters in `stompman.Client()` to your needs.

- When connection is lost, stompman will attempt to handle it automatically. `stompman.FailedAllConnectAttemptsError` will be raised if all connection attempts fail. `stompman.FailedAllWriteAttemptsError` will be raised if connection succeeds but sending a frame or heartbeat lead to losing connection.
- Set `keep_alive_on_connection_failure=True` to keep background heartbeat and read recovery running after a retry cycle is exhausted. The default remains `False`, and errors from `Client.send()` still follow `connect_retry_attempts` and `write_retry_attempts`.
- Connections that succeed and immediately fail are spaced by `connect_retry_interval` as well, preventing a tight reconnect loop.
- If no messages are received for `no_message_restart_interval` (defaults to 1 hour), stompman will force a reconnect. Set to `None` to disable.
- To implement health checks, use `stompman.Client.is_alive()` — it will return `True` if everything is OK and `False` if server is not responding.
- `stompman` logs exhausted background recovery and invalid ACK/NACK state. Use `client.core.status` to inspect the current connection generation and failure.

### ...and caveats

- stompman supports Python 3.11 and newer.
- It implements [STOMP 1.2](https://stomp.github.io/stomp-specification-1.2.html) — the latest version of the protocol.
- Heartbeats are negotiated with the broker and sent automatically in the background (defaults to 1 second). A zero interval disables the corresponding heartbeat direction.

Also, I want to pointed out that:

- Protocol parsing is inspired by [aiostomp](https://github.com/pedrokiefer/aiostomp/blob/3449dcb53f43e5956ccc7662bb5b7d76bc6ef36b/aiostomp/protocol.py) (meaning: consumed by me and refactored from).
- stompman is tested and used with [ActiveMQ Artemis](https://activemq.apache.org/components/artemis/) and [ActiveMQ Classic](https://activemq.apache.org/components/classic/).
    - Caveat: a message sent by a Stomp client is converted into a JMS `TextMessage`/`BytesMessage` based on the `content-length` header (see the docs [here](https://activemq.apache.org/components/classic/documentation/stomp)). In order to send a `TextMessage`, `Client.send` needs to be invoked with `add_content_length` header set to `False`
- CONNECT and CONNECTED headers remain literal, as required by STOMP 1.2. Other frame headers escape carriage returns, line feeds, colons and backslashes.

### FastStream STOMP broker

[An implementation of STOMP broker for FastStream.](packages/faststream-stomp/README.md)

### Examples

See examples in [examples/](examples).

## Core runtime and broker receipts

The FastStream facade and the legacy `Client` facade share an independent runtime.
See [the architecture and migration notes](docs/session-core.md) for ownership,
recovery, delivery capacity and compatibility details.

Existing `Client.send()` calls still return `None` after an unconfirmed write.
Request a broker receipt explicitly when acceptance confirmation is needed:

```python
receipt = await client.send(b"payload", "events", receipt_timeout=5)

async with client.begin(receipt_timeout=5) as transaction:
    await transaction.send(b"first", "events")
    await transaction.send(b"second", "events")
```

Receipts confirm broker acceptance, not consumer processing. A missing receipt
can leave an operation's outcome uncertain; the library does not automatically
replay that operation. An ambiguous transaction commit raises
`TransactionOutcomeUnknownError` instead of risking a duplicate commit. Open
transactions are restored after reconnection with BEGIN and their send journal.

Delivery admission defaults to 1,024 messages and 64 MiB, including unsettled
messages. Configure `max_pending_messages`, `max_pending_bytes` and broker
credit/prefetch together. Handler saturation leaves the session reader responsive;
exhausted admission raises `ConsumerOverloadedError`. Use `client-individual` ACK
when messages must remain eligible for broker redelivery after failure.
