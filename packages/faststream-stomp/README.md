# FastStream STOMP broker

## How To Use

Install the package:

```sh
uv add faststream-stomp
poetry add faststream-stomp
```

Basic usage:

```python
import asyncio

import faststream
import faststream_stomp
import stompman

server = stompman.ConnectionParameters(host="127.0.0.1", port=61616, login="admin", passcode="password")
broker = faststream_stomp.StompBroker(servers=[server])


@broker.subscriber("first")
@broker.publisher("second")
def _(message: str) -> str:
    print(message)  # this will print message from startup
    return "Hi from first handler!"


@broker.subscriber("second")
def _(message: str) -> None:
    print(message)  # this will print message from first handler


app = faststream.FastStream(broker)


@app.after_startup
async def send_first_message() -> None:
    await broker.connect()
    await broker.publish("Hi from startup!", "first")


if __name__ == "__main__":
    asyncio.run(app.run())
```

Also there are `StompRouter` and `TestStompBroker` for testing. It works similarly to built-in brokers from FastStream, I recommend to read the original [FastStream documentation](https://faststream.airt.ai/latest/getting-started).

For connection, retry, heartbeat, and delivery-capacity options, pass a core configuration:

```python
from stompman.core.config import RuntimeConfig

broker = faststream_stomp.StompBroker(
    RuntimeConfig(
        [server],
        connect_retry_attempts=5,
        max_concurrent_handlers=100,
        max_pending_messages=1000,
        max_pending_bytes=64 * 1024 * 1024,
    )
)
```

You can also pass an existing `stompman.core.runtime.Runtime`. The broker starts and closes that runtime;
`broker.runtime.status` exposes connection state and delivery capacity. Existing `StompBroker(stompman.Client(...))`
construction remains supported: the broker copies the Client's configuration once and creates its own runtime.
It does not execute Client methods or share the Client's connection or lifecycle.

By default, published frames include the STOMP `content-length` header. You can change this for the whole broker or
override it for a publisher or individual publish call:

```python
broker = faststream_stomp.StompBroker(servers=[server], add_content_length=False)
publisher = broker.publisher("events", add_content_length=True)

await broker.publish("text message", "events", add_content_length=False)
await publisher.publish("bytes message")
```

Use `reply_add_content_length` on `subscriber()` when automatic replies need a different setting. The same options
are available for batch publishing and delayed `StompRoute`/`StompRoutePublisher` registrations. Per-call settings
override publisher or subscriber reply defaults, which in turn override the broker default.

An incoming `reply-to` header enables automatic replies to that destination. Replies retain the incoming correlation ID.

### Caveats

- When exception is raised in consumer handler, the message will be nacked (FastStream doesn't do this by default)
