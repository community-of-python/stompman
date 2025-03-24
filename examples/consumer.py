import asyncio

import stompman

server = stompman.ConnectionParameters(host="127.0.0.1", port=9000, login="admin", passcode=":=123")


async def handle_message(message_frame: stompman.MessageFrame) -> None:
    message_content = message_frame.body.decode()

    if "Hi" not in message_content:
        error_message = "Producer is not friendly :("
        raise ValueError(error_message)

    await asyncio.sleep(0.1)
    print(f"received and processed friendly message: {message_content}")  # noqa: T201


def handle_suppressed_exception(exception: Exception, message_frame: stompman.MessageFrame) -> None:
    print(f"caught an exception, perhaps, producer is not friendly: {message_frame.body=!r} {exception=}")  # noqa: T201


async def main() -> None:
    async with stompman.Client(servers=[server]) as client:
        await client.subscribe("DLQ", handler=handle_message, on_suppressed_exception=handle_suppressed_exception)


if __name__ == "__main__":
    asyncio.run(main())
