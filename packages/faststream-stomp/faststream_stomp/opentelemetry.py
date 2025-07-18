import stompman
from faststream._internal.basic_types import AnyDict
from faststream.message import StreamMessage
from faststream.opentelemetry import TelemetrySettingsProvider
from faststream.opentelemetry.consts import MESSAGING_DESTINATION_PUBLISH_NAME
from faststream.opentelemetry.middleware import TelemetryMiddleware
from opentelemetry.metrics import Meter, MeterProvider
from opentelemetry.semconv._incubating.attributes import messaging_attributes  # noqa: PLC2701
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.trace import TracerProvider

from faststream_stomp.publisher import StompProducerPublishKwargs


class StompTelemetrySettingsProvider(TelemetrySettingsProvider[stompman.MessageFrame]):
    messaging_system = "stomp"

    def get_consume_attrs_from_message(self, msg: StreamMessage[stompman.MessageFrame]) -> "AnyDict":
        return {
            messaging_attributes.MESSAGING_SYSTEM: self.messaging_system,
            messaging_attributes.MESSAGING_MESSAGE_ID: msg.message_id,
            messaging_attributes.MESSAGING_MESSAGE_CONVERSATION_ID: msg.correlation_id,
            SpanAttributes.MESSAGING_MESSAGE_PAYLOAD_SIZE_BYTES: len(msg.body),
            MESSAGING_DESTINATION_PUBLISH_NAME: msg.raw_message.headers["destination"],
        }

    def get_consume_destination_name(self, msg: StreamMessage[stompman.MessageFrame]) -> str:  # noqa: PLR6301
        return msg.raw_message.headers["destination"]

    def get_publish_attrs_from_kwargs(self, kwargs: StompProducerPublishKwargs) -> AnyDict:  # type: ignore[override]
        publish_attrs = {
            messaging_attributes.MESSAGING_SYSTEM: self.messaging_system,
            messaging_attributes.MESSAGING_DESTINATION_NAME: kwargs["destination"],
        }
        if kwargs["correlation_id"]:
            publish_attrs[messaging_attributes.MESSAGING_MESSAGE_CONVERSATION_ID] = kwargs["correlation_id"]
        return publish_attrs

    def get_publish_destination_name(self, kwargs: StompProducerPublishKwargs) -> str:  # type: ignore[override]  # noqa: PLR6301
        return kwargs["destination"]


class StompTelemetryMiddleware(TelemetryMiddleware):
    def __init__(
        self,
        *,
        tracer_provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
        meter: Meter | None = None,
    ) -> None:
        super().__init__(
            settings_provider_factory=lambda _: StompTelemetrySettingsProvider(),
            tracer_provider=tracer_provider,
            meter_provider=meter_provider,
            meter=meter,
            include_messages_counters=False,
        )
