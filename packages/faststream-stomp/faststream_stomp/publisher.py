import typing
from typing import Any, NoReturn

import stompman
from fast_depends.library.serializer import SerializerProto
from faststream import PublishCommand, PublishType
from faststream._internal.basic_types import SendableMessage
from faststream._internal.configs import BrokerConfig
from faststream._internal.endpoint.publisher import PublisherSpecification, PublisherUsecase
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

    def __init__(self, *, client: stompman.Client, serializer: SerializerProto | None) -> None:
        self.client = client
        self.serializer = serializer

    async def publish(self, cmd: StompPublishCommand) -> None:
        body, content_type = encode_message(cmd.body, serializer=self.serializer)
        all_headers = cmd.headers.copy() if cmd.headers else {}
        if cmd.correlation_id:
            all_headers["correlation-id"] = cmd.correlation_id
        await self.client.send(body, cmd.destination, content_type=content_type, headers=all_headers)

    async def request(self, cmd: StompPublishCommand) -> NoReturn:
        msg = "`StompProducer` can be used only to publish a response for `reply-to` or `RPC` messages."
        raise NotImplementedError(msg)

    async def publish_batch(self, cmd: StompPublishCommand) -> NoReturn:
        raise NotImplementedError


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
        publish_command = StompPublishCommand.from_cmd(cmd)
        publish_command.destination = self.config.full_destination
        return typing.cast(
            "None",
            await self._basic_publish(
                publish_command, producer=self.config._outer_config.producer, _extra_middlewares=_extra_middlewares
            ),
        )

    async def publish(
        self, message: SendableMessage, *, correlation_id: str | None = None, headers: dict[str, str] | None = None
    ) -> None:
        publish_command = StompPublishCommand(
            message,
            _publish_type=PublishType.PUBLISH,
            destination=self.config.full_destination,
            correlation_id=correlation_id,
            headers=headers,
        )
        return typing.cast(
            "None",
            await self._basic_publish(
                publish_command, producer=self.config._outer_config.producer, _extra_middlewares=()
            ),
        )

    async def request(
        self, message: SendableMessage, *, correlation_id: str | None = None, headers: dict[str, str] | None = None
    ) -> Any:  # noqa: ANN401
        publish_command = StompPublishCommand(
            message,
            _publish_type=PublishType.REQUEST,
            destination=self.config.full_destination,
            correlation_id=correlation_id,
            headers=headers,
        )
        return await self._basic_request(publish_command, producer=self.config._outer_config.producer)
