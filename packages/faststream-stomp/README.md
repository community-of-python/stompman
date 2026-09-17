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
broker = faststream_stomp.StompBroker(stompman.Client([server]))


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

By default, published frames include the STOMP `content-length` header. You can change this for the whole broker or
override it for a publisher or individual publish call:

```python
broker = faststream_stomp.StompBroker(stompman.Client([server]), add_content_length=False)
publisher = broker.publisher("events", add_content_length=True)

await broker.publish("text message", "events", add_content_length=False)
await publisher.publish("bytes message")
```

Use `reply_add_content_length` on `subscriber()` when automatic replies need a different setting. The same options
are available for batch publishing and delayed `StompRoute`/`StompRoutePublisher` registrations. Per-call settings
override publisher or subscriber reply defaults, which in turn override the broker default.

### Confirming publication

By default `publish()` returns once the frame has been written to the socket, which does not mean the broker
accepted the message. Pass `receipt_timeout` to wait for the broker's STOMP `RECEIPT` instead. It follows the same precedence as
`add_content_length`: per-call, then publisher, then broker default.

```python
broker = faststream_stomp.StompBroker(stompman.Client([server]), receipt_timeout=3.0)
publisher = broker.publisher("events", receipt_timeout=5.0)

await broker.publish("text message", "events", receipt_timeout=1.0)
await publisher.publish("another message")
```

The default is `None` everywhere, which preserves the existing behavior. A failed confirmation raises
`stompman.SendReceiptError` from `publish()`, with `reason` equal to `rejected`, `timeout`, or `connection_lost`.

`timeout` and `connection_lost` are ambiguous outcomes: the broker may have accepted the message even though its
receipt was lost. Nothing is replayed automatically: give each application event a stable ID and deduplicate in the
broker or downstream consumer if you retry. `JMSCorrelationID` alone does not deduplicate anything. And a receipt only
means the broker took ownership: it does not mean a consumer has processed the message.

`receipt_timeout` is not available for batch publishing, which publishes inside a transaction: a receipt for a
transactional `SEND` says nothing about whether the `COMMIT` succeeded. A broker or publisher default is ignored there
rather than silently claiming confirmation.

### Caveats

- When exception is raised in consumer handler, the message will be nacked (FastStream doesn't do this by default)
