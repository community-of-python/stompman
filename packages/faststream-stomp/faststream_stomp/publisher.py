import typing
from typing import Any, NoReturn

import stompman
from fast_depends.library.serializer import SerializerProto
from faststream import PublishCommand, PublishType
from faststream._internal.basic_types import SendableMessage
from faststream._internal.configs import BrokerConfig
from faststream._internal.endpoint.publisher import PublisherSpecification, PublisherUsecase
from faststream._internal.parser import CodecProto, DefaultCodec
from faststream._internal.producer import ProducerProto
from faststream._internal.types import AsyncCallable, PublisherMiddleware
from faststream.message import encode_message
from faststream.specification.asyncapi.utils import resolve_payloads
from faststream.specification.schema import Message, Operation, PublisherSpec

from faststream_stomp.models import (
    StompPublishCommand,
    StompPublisherSpecificationConfig,
    StompPublisherUsecaseConfig,
)


class StompProducer(ProducerProto[StompPublishCommand]):
    _parser: AsyncCallable
    _decoder: AsyncCallable

    def __init__(
        self,
        *,
        client: stompman.Client,
        serializer: SerializerProto | None,
        add_content_length: bool = True,
    ) -> None:
        self.client = client
        self.serializer = serializer
        self.add_content_length = add_content_length
        self.codec: CodecProto = DefaultCodec()

    async def publish(self, cmd: StompPublishCommand) -> None:
        body, content_type = encode_message(cmd.body, serializer=self.serializer)
        await self.client.send(
            body,
            cmd.destination,
            content_type=content_type,
            add_content_length=self._resolve_add_content_length(cmd),
            headers=_make_headers_for_publish(cmd),
        )

    async def request(self, cmd: StompPublishCommand) -> NoReturn:
        msg = "`StompProducer` can be used only to publish a response for `reply-to` or `RPC` messages."
        raise NotImplementedError(msg)

    async def publish_batch(self, cmd: StompPublishCommand) -> None:
        async with self.client.begin() as transaction:
            for one_body in cmd.batch_bodies:
                body, content_type = encode_message(one_body, serializer=self.serializer)
                await transaction.send(
                    body,
                    cmd.destination,
                    content_type=content_type,
                    add_content_length=self._resolve_add_content_length(cmd),
                    headers=_make_headers_for_publish(cmd),
                )

    def _resolve_add_content_length(self, cmd: StompPublishCommand) -> bool:
        return self.add_content_length if cmd.add_content_length is None else cmd.add_content_length


def _make_headers_for_publish(cmd: StompPublishCommand) -> dict[str, str]:
    all_headers = cmd.headers.copy()
    if cmd.correlation_id:
        all_headers["correlation-id"] = cmd.correlation_id
    return all_headers


class StompPublisherSpecification(PublisherSpecification[BrokerConfig, StompPublisherSpecificationConfig]):
    @property
    def name(self) -> str:
        return f"{self._outer_config.prefix}{self.config.destination_without_prefix}:Publisher"

    def get_schema(self) -> dict[str, PublisherSpec]:
        return {
            self.name: PublisherSpec(
                description=self.config.description_,
                operation=Operation(
                    message=Message(
                        title=f"{self.name}:Message", payload=resolve_payloads(self.get_payloads(), "Publisher")
                    ),
                    bindings=None,
                ),
                bindings=None,
            )
        }


class StompPublisher(PublisherUsecase):
    def __init__(self, config: StompPublisherUsecaseConfig, specification: StompPublisherSpecification) -> None:
        self.config = config
        super().__init__(config=config, specification=specification)  # type: ignore[arg-type]

    async def _publish(
        self, cmd: PublishCommand, *, _extra_middlewares: typing.Iterable[PublisherMiddleware[PublishCommand]]
    ) -> None:
        publish_command = StompPublishCommand.from_cmd(cmd, add_content_length=self.config.add_content_length)
        publish_command.destination = self.config.full_destination
        return typing.cast(
            "None",
            await self._basic_publish(
                publish_command, producer=self.config._outer_config.producer, _extra_middlewares=_extra_middlewares
            ),
        )

    async def publish(
        self,
        message: SendableMessage,
        *,
        correlation_id: str | None = None,
        headers: dict[str, str] | None = None,
        add_content_length: bool | None = None,
    ) -> None:
        publish_command = StompPublishCommand(
            message,
            _publish_type=PublishType.PUBLISH,
            destination=self.config.full_destination,
            correlation_id=correlation_id,
            headers=headers,
            add_content_length=self.config.add_content_length if add_content_length is None else add_content_length,
        )
        return typing.cast(
            "None",
            await self._basic_publish(
                publish_command, producer=self.config._outer_config.producer, _extra_middlewares=()
            ),
        )

    async def request(
        self,
        message: SendableMessage,
        *,
        correlation_id: str | None = None,
        headers: dict[str, str] | None = None,
        add_content_length: bool | None = None,
    ) -> Any:  # noqa: ANN401
        publish_command = StompPublishCommand(
            message,
            _publish_type=PublishType.REQUEST,
            destination=self.config.full_destination,
            correlation_id=correlation_id,
            headers=headers,
            add_content_length=self.config.add_content_length if add_content_length is None else add_content_length,
        )
        return await self._basic_request(publish_command, producer=self.config._outer_config.producer)

    async def publish_batch(
        self,
        *messages: SendableMessage,
        correlation_id: str | None = None,
        headers: dict[str, str] | None = None,
        add_content_length: bool | None = None,
    ) -> None:
        publish_command = StompPublishCommand(
            *messages,
            _publish_type=PublishType.PUBLISH,
            destination=self.config.full_destination,
            correlation_id=correlation_id,
            headers=headers,
            add_content_length=self.config.add_content_length if add_content_length is None else add_content_length,
        )
        return typing.cast(
            "None",
            await self._basic_publish_batch(
                publish_command, producer=self.config._outer_config.producer, _extra_middlewares=()
            ),
        )
