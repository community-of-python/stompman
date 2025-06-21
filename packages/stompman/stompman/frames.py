from dataclasses import dataclass
from typing import Literal, NotRequired, Self, TypedDict

ConnectHeaders = TypedDict(
    "ConnectHeaders",
    {
        "accept-version": str,
        "host": str,
        "login": NotRequired[str],
        "passcode": NotRequired[str],
        "heart-beat": NotRequired[str],
        "content-length": NotRequired[str],
    },
)
ConnectedHeaders = TypedDict(
    "ConnectedHeaders",
    {
        "version": str,
        "server": NotRequired[str],
        "heart-beat": NotRequired[str],
        "content-length": NotRequired[str],
    },
)
SendHeaders = TypedDict(
    "SendHeaders",
    {
        "content-length": NotRequired[str],
        "content-type": NotRequired[str],
        "destination": str,
        "transaction": NotRequired[str],
    },
)
AckMode = Literal["client", "client-individual", "auto"]
SubscribeHeaders = TypedDict(
    "SubscribeHeaders",
    {
        "id": str,
        "destination": str,
        "ack": NotRequired[AckMode],
        "content-length": NotRequired[str],
    },
)
UnsubscribeHeaders = TypedDict(
    "UnsubscribeHeaders",
    {
        "id": str,
        "content-length": NotRequired[str],
    },
)
AckHeaders = TypedDict(
    "AckHeaders",
    {
        "subscription": str,
        "id": str,
        "transaction": NotRequired[str],
        "content-length": NotRequired[str],
    },
)
NackHeaders = TypedDict(
    "NackHeaders",
    {
        "subscription": str,
        "id": str,
        "transaction": NotRequired[str],
        "content-length": NotRequired[str],
    },
)
BeginHeaders = TypedDict(
    "BeginHeaders",
    {
        "transaction": str,
        "content-length": NotRequired[str],
    },
)
CommitHeaders = TypedDict(
    "CommitHeaders",
    {
        "transaction": str,
        "content-length": NotRequired[str],
    },
)
AbortHeaders = TypedDict(
    "AbortHeaders",
    {
        "transaction": str,
        "content-length": NotRequired[str],
    },
)
DisconnectHeaders = TypedDict(
    "DisconnectHeaders",
    {
        "receipt": NotRequired[str],
        "content-length": NotRequired[str],
    },
)
ReceiptHeaders = TypedDict(
    "ReceiptHeaders",
    {
        "receipt-id": str,
        "content-length": NotRequired[str],
    },
)
MessageHeaders = TypedDict(
    "MessageHeaders",
    {
        "destination": str,
        "message-id": str,
        "subscription": str,
        "ack": NotRequired[str],
        "content-type": NotRequired[str],
        "content-length": NotRequired[str],
    },
)
ErrorHeaders = TypedDict(
    "ErrorHeaders",
    {
        "message": str,
        "content-length": NotRequired[str],
        "content-type": NotRequired[str],
    },
)


@dataclass(frozen=True, kw_only=True, slots=True)
class ConnectFrame:
    headers: ConnectHeaders


@dataclass(frozen=True, kw_only=True, slots=True)
class StompFrame:
    headers: ConnectHeaders


@dataclass(frozen=True, kw_only=True, slots=True)
class ConnectedFrame:
    headers: ConnectedHeaders


@dataclass(frozen=True, kw_only=True, slots=True)
class SendFrame:
    headers: SendHeaders
    body: bytes = b""

    @classmethod
    def build(
        cls,
        *,
        body: bytes,
        destination: str,
        transaction: str | None,
        content_type: str | None,
        add_content_length: bool,
        headers: dict[str, str] | None,
    ) -> Self:
        all_headers: SendHeaders = headers or {}  # type: ignore[assignment]
        all_headers["destination"] = destination
        if add_content_length:
            all_headers["content-length"] = str(len(body))
        if content_type is not None:
            all_headers["content-type"] = content_type
        if transaction is not None:
            all_headers["transaction"] = transaction
        return cls(headers=all_headers, body=body)


@dataclass(frozen=True, kw_only=True, slots=True)
class SubscribeFrame:
    headers: SubscribeHeaders

    @classmethod
    def build(cls, *, subscription_id: str, destination: str, ack: AckMode, headers: dict[str, str] | None) -> Self:
        all_headers: SubscribeHeaders = headers.copy() if headers else {}  # type: ignore[assignment, typeddict-item]
        all_headers.update({"id": subscription_id, "destination": destination, "ack": ack})
        return cls(headers=all_headers)


@dataclass(frozen=True, kw_only=True, slots=True)
class UnsubscribeFrame:
    headers: UnsubscribeHeaders


@dataclass(frozen=True, kw_only=True, slots=True)
class AckFrame:
    headers: AckHeaders


@dataclass(frozen=True, kw_only=True, slots=True)
class NackFrame:
    headers: NackHeaders


@dataclass(frozen=True, kw_only=True, slots=True)
class BeginFrame:
    headers: BeginHeaders


@dataclass(frozen=True, kw_only=True, slots=True)
class CommitFrame:
    headers: CommitHeaders


@dataclass(frozen=True, kw_only=True, slots=True)
class AbortFrame:
    headers: AbortHeaders


@dataclass(frozen=True, kw_only=True, slots=True)
class DisconnectFrame:
    headers: DisconnectHeaders


@dataclass(frozen=True, kw_only=True, slots=True)
class ReceiptFrame:
    headers: ReceiptHeaders


@dataclass(frozen=True, kw_only=True, slots=True)
class MessageFrame:
    headers: MessageHeaders
    body: bytes


@dataclass(frozen=True, kw_only=True, slots=True)
class ErrorFrame:
    headers: ErrorHeaders
    body: bytes = b""


@dataclass(frozen=True, kw_only=True, slots=True)
class HeartbeatFrame: ...


AnyClientFrame = (
    SendFrame
    | SubscribeFrame
    | UnsubscribeFrame
    | BeginFrame
    | CommitFrame
    | AbortFrame
    | AckFrame
    | NackFrame
    | DisconnectFrame
    | ConnectFrame
    | StompFrame
)
AnyRealServerFrame = ConnectedFrame | MessageFrame | ReceiptFrame | ErrorFrame
AnyServerFrame = AnyRealServerFrame | HeartbeatFrame
