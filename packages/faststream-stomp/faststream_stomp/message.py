from typing import Self, cast

import stompman
from faststream.message import StreamMessage, gen_cor_id


class StompStreamMessage(StreamMessage[stompman.AckableMessageFrame]):
    async def ack(self) -> None:
        if not self.committed:
            await self.raw_message.ack()
        return await super().ack()

    async def nack(self) -> None:
        if not self.committed:
            await self.raw_message.nack()
        return await super().nack()

    async def reject(self) -> None:
        if not self.committed:
            await self.raw_message.nack()
        return await super().reject()

    @classmethod
    async def from_frame(cls, message: stompman.AckableMessageFrame) -> Self:
        return cls(
            raw_message=message,
            body=message.body,
            headers=cast("dict[str, str]", message.headers),
            content_type=message.headers.get("content-type"),
            message_id=message.headers["message-id"],
            correlation_id=cast("str", message.headers.get("correlation-id", gen_cor_id())),
        )
