import asyncio

import faker as faker_
import faststream_stomp
import stompman
from faststream import Context, FastStream

server = stompman.ConnectionParameters(host="127.0.0.1", port=9000, login="admin", passcode=":=123")
broker = faststream_stomp.StompBroker(stompman.Client([server]))
faker = faker_.Faker()
expected_body, prefix, destination = faker.pystr(), faker.pystr(), faker.pystr()


def route(body: str, message: stompman.MessageFrame = Context("message.raw_message")) -> None:  # noqa: B008
    assert body == expected_body
    print("GOT MESSAGE")
    event.set()


router = faststream_stomp.StompRouter(prefix=prefix, handlers=(faststream_stomp.StompRoute(route, destination),))
publisher = router.publisher(destination)

broker.include_router(router)
app = FastStream(broker)
event = asyncio.Event()


@app.after_startup
async def _() -> None:
    await broker.connect()
    await publisher.publish(expected_body)


if __name__ == "__main__":
    asyncio.run(app.run())
