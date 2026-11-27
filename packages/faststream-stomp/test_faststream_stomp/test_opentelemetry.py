from unittest.mock import AsyncMock

import pytest
from faststream import ContextRepo, StreamMessage
from faststream.opentelemetry.consts import MESSAGING_DESTINATION_PUBLISH_NAME
from faststream_stomp.opentelemetry import StompTelemetryMiddleware
from opentelemetry.sdk.trace import SpanLimits, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.semconv._incubating.attributes.messaging_attributes import MESSAGING_DESTINATION_NAME
from opentelemetry.semconv.trace import SpanAttributes
from opentelemetry.trace import SpanKind
from stompman import MessageFrame


@pytest.mark.anyio
@pytest.mark.parametrize(
    "max_attributes",
    [pytest.param(None, id="default-limit"), pytest.param(2, id="two-attribute-slots")],
)
async def test_consumer_spans_export_destination(max_attributes: int | None) -> None:
    destination = "orders.incoming"
    frame = MessageFrame(
        headers={"destination": destination, "message-id": "message-1", "subscription": "subscription-1"},
        body=b"fixture",
    )
    message = StreamMessage(raw_message=frame, body=frame.body, message_id=frame.headers["message-id"])
    exporter = InMemorySpanExporter()
    provider = TracerProvider(span_limits=SpanLimits(max_span_attributes=max_attributes))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    scope = StompTelemetryMiddleware(tracer_provider=provider)(frame, context=ContextRepo())
    handler = AsyncMock(return_value="handled")

    try:
        async with scope:
            assert await scope.consume_scope(handler, message) == "handled"
        handler.assert_awaited_once_with(message)

        create, process = exporter.get_finished_spans()
        assert create.name == f"{destination} create"
        assert process.name == f"{destination} process"
        assert create.context is not None
        assert process.parent is not None
        assert process.parent.span_id == create.context.span_id

        for span in (create, process):
            assert span.kind is SpanKind.CONSUMER
            assert span.attributes is not None
            assert span.attributes[MESSAGING_DESTINATION_PUBLISH_NAME] == destination
            if max_attributes is None:
                assert span.attributes[MESSAGING_DESTINATION_NAME] == destination
                assert span.attributes[SpanAttributes.MESSAGING_SYSTEM] == "stomp"
            else:
                assert MESSAGING_DESTINATION_NAME not in span.attributes

        assert process.attributes is not None
        assert process.attributes[SpanAttributes.MESSAGING_OPERATION] == "process"
        assert create.attributes is not None
        assert create.attributes[SpanAttributes.MESSAGING_MESSAGE_PAYLOAD_SIZE_BYTES] == len(frame.body)
    finally:
        provider.shutdown()
