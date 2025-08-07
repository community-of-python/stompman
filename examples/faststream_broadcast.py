import asyncio

import faststream
import faststream_stomp
import stompman

server = stompman.ConnectionParameters(host="127.0.0.1", port=9000, login="admin", passcode=":=123")
broker = faststream_stomp.StompBroker(stompman.Client([server]))

count = 0


@broker.subscriber("first")
async def _(message: str) -> None:
    global count
    count += 1
    print(f"started {count}")
    await asyncio.sleep(1)
    print(f"done {count}")
    # print(message)


app = faststream.FastStream(broker)


@app.after_startup
async def send_first_message() -> None:
    await broker.connect()
    for _ in range(1000):
        await broker.publish("Hi from startup!", "first")


if __name__ == "__main__":
    asyncio.run(app.run())
