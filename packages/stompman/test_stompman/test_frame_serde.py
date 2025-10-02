import pytest
from stompman import (
    AckFrame,
    AnyClientFrame,
    AnyServerFrame,
    ConnectedFrame,
    ConnectFrame,
    ErrorFrame,
    FrameParser,
    HeartbeatFrame,
    MessageFrame,
    dump_frame,
)
from stompman.serde import NEWLINE


@pytest.mark.parametrize(
    ("frame", "dumped_frame"),
    [
        (AckFrame(headers={"subscription": "1", "id": "1"}), (b"ACK\nid:1\nsubscription:1\n\n\x00")),
        (ConnectedFrame(headers={"version": "1.1"}), (b"CONNECTED\nversion:1.1\n\n\x00")),
        (
            MessageFrame(
                headers={"destination": "me:123", "message-id": "you\nmoreextra\\here", "subscription": "hi"},
                body=b"I Am The Walrus",
            ),
            (
                b"MESSAGE\ndestination:me\\c123\nmessage-id:you\\nmoreextra\\\\here\nsubscription:hi\n\n"
                b"I Am The Walrus\x00"
            ),
        ),
    ],
)
def test_dump_frame(frame: AnyClientFrame, dumped_frame: bytes) -> None:
    assert dump_frame(frame) == dumped_frame


@pytest.mark.parametrize(
    ("raw_frames", "loaded_frames"),
    [
        # Partial packet
        (
            b"CONNECT\naccept-version:1.0\n\n\x00",
            [ConnectFrame(headers={"accept-version": "1.0"})],  # type: ignore[typeddict-item]
        ),
        # Full packet
        (
            b"MESSAGE\naccept-version:1.0\n\nHey dude\x00",
            [MessageFrame(headers={"accept-version": "1.0"}, body=b"Hey dude")],  # type: ignore[typeddict-item]
        ),
        # Long packet
        (
            (
                b"MESSAGE\n"
                b"content-length:14\nexpires:0\ndestination:/topic/"
                b"xxxxxxxxxxxxxxxxxxxxxxxxxl"
                b"\nsubscription:1\npriority:4\nActiveMQ.MQTT.QoS:1\nmessage-id"
                b":ID\\cxxxxxx-35207-1543430467768-204"
                b"\\c363\\c-1\\c1\\c463859\npersistent:true\ntimestamp"
                b":1548945234003\n\n222.222.22.222"
                b"\x00\nMESSAGE\ncontent-length:12\nexpires:0\ndestination:"
                b"/topic/xxxxxxxxxxxxxxxxxxxxxxxxxx"
                b"\nsubscription:1\npriority:4\nActiveMQ.MQTT.QoS:1\nmessage-id"
                b":ID\\cxxxxxx-35207-1543430467768-204"
                b"\\c363\\c-1\\c1\\c463860\npersistent:true\ntimestamp"
                b":1548945234005\n\n88.88.888.88"
                b"\x00\nMESSAGE\ncontent-length:11\nexpires:0\ndestination:"
                b"/topic/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                b"\nsubscription:1\npriority:4\nActiveMQ.MQTT.QoS:1\nmessage-id"
                b":ID\\cxxxxxx-35207-1543430467768-204"
                b"\\c362\\c-1\\c1\\c290793\npersistent:true\ntimestamp"
                b":1548945234005\n\n111.11.1.11"
                b"\x00\nMESSAGE\ncontent-length:14\nexpires:0\ndestination:"
                b"/topic/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                b"\nsubscription:1\npriority:4\nActiveMQ.MQTT.QoS:1\nmessage-id"
                b":ID\\cxxxxxx-35207-1543430467768-204"
                b"\\c362\\c-1\\c1\\c290794\npersistent:true\ntimestamp:"
                b"1548945234005\n\n222.222.22.222"
                b"\x00\nMESSAGE\ncontent-length:12\nexpires:0\ndestination:"
                b"/topic/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                b"\nsubscription:1\npriority:4\nActiveMQ.MQTT.QoS:1\nmessage-id"
                b":ID\\cxxxxxx-35207-1543430467768-204"
                b"\\c362\\c-1\\c1\\c290795\npersistent:true\ntimestamp:"
                b"1548945234005\n\n88.88.888.88\x00\nMESS"
            ),
            [
                MessageFrame(
                    headers={  # type: ignore[typeddict-unknown-key]
                        "content-length": "14",
                        "expires": "0",
                        "destination": "/topic/xxxxxxxxxxxxxxxxxxxxxxxxxl",
                        "subscription": "1",
                        "priority": "4",
                        "ActiveMQ.MQTT.QoS": "1",
                        "message-id": "ID:xxxxxx-35207-1543430467768-204:363:-1:1:463859",
                        "persistent": "true",
                        "timestamp": "1548945234003",
                    },
                    body=b"222.222.22.222",
                ),
                HeartbeatFrame(),
                MessageFrame(
                    headers={  # type: ignore[typeddict-unknown-key]
                        "content-length": "12",
                        "expires": "0",
                        "destination": "/topic/xxxxxxxxxxxxxxxxxxxxxxxxxx",
                        "subscription": "1",
                        "priority": "4",
                        "ActiveMQ.MQTT.QoS": "1",
                        "message-id": "ID:xxxxxx-35207-1543430467768-204:363:-1:1:463860",
                        "persistent": "true",
                        "timestamp": "1548945234005",
                    },
                    body=b"88.88.888.88",
                ),
                HeartbeatFrame(),
                MessageFrame(
                    headers={  # type: ignore[typeddict-unknown-key]
                        "content-length": "11",
                        "expires": "0",
                        "destination": "/topic/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                        "subscription": "1",
                        "priority": "4",
                        "ActiveMQ.MQTT.QoS": "1",
                        "message-id": "ID:xxxxxx-35207-1543430467768-204:362:-1:1:290793",
                        "persistent": "true",
                        "timestamp": "1548945234005",
                    },
                    body=b"111.11.1.11",
                ),
                HeartbeatFrame(),
                MessageFrame(
                    headers={  # type: ignore[typeddict-unknown-key]
                        "content-length": "14",
                        "expires": "0",
                        "destination": "/topic/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                        "subscription": "1",
                        "priority": "4",
                        "ActiveMQ.MQTT.QoS": "1",
                        "message-id": "ID:xxxxxx-35207-1543430467768-204:362:-1:1:290794",
                        "persistent": "true",
                        "timestamp": "1548945234005",
                    },
                    body=b"222.222.22.222",
                ),
                HeartbeatFrame(),
                MessageFrame(
                    headers={  # type: ignore[typeddict-unknown-key]
                        "content-length": "12",
                        "expires": "0",
                        "destination": "/topic/xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                        "subscription": "1",
                        "priority": "4",
                        "ActiveMQ.MQTT.QoS": "1",
                        "message-id": "ID:xxxxxx-35207-1543430467768-204:362:-1:1:290795",
                        "persistent": "true",
                        "timestamp": "1548945234005",
                    },
                    body=b"88.88.888.88",
                ),
                HeartbeatFrame(),
            ],
        ),
        # Partial packet #2
        (
            b"CONNECT\naccept-version:1.0\n\n\x00\nCONNECTED\nversion:1.0\n\n\x00\n",
            [
                ConnectFrame(headers={"accept-version": "1.0"}),  # type: ignore[typeddict-item]
                HeartbeatFrame(),
                ConnectedFrame(headers={"version": "1.0"}),
                HeartbeatFrame(),
            ],
        ),
        # Utf-8
        (
            b"CONNECTED\naccept-version:1.0\n\n\x00\nERROR\nheader:1.0\n\n\xc3\xa7\x00\n",
            [
                ConnectedFrame(headers={"accept-version": "1.0"}),  # type: ignore[typeddict-item]
                HeartbeatFrame(),
                ErrorFrame(headers={"header": "1.0"}, body="ç".encode()),  # type: ignore[typeddict-item]
                HeartbeatFrame(),
            ],
        ),
        (NEWLINE, [HeartbeatFrame()]),
        # Two headers: only first should be accepted
        (
            b"CONNECTED\naccept-version:1.0\naccept-version:1.1\n\n\x00",
            [ConnectedFrame(headers={"accept-version": "1.0"})],  # type: ignore[typeddict-item]
        ),
        # no end of line after command
        (b"CONNECTED", []),
        (b"CONNECTED\n", []),
        (b"CONNECTED\x00", []),
        # \r\n after command
        (b"CONNECTED\r\n\n\n\x00", [ConnectedFrame(headers={})]),  # type: ignore[typeddict-item]
        (b"CONNECTED\r\nheader:1.0\n\n\x00", [ConnectedFrame(headers={"header": "1.0"})]),  # type: ignore[typeddict-item]
        # header without :
        (b"CONNECTED\nhead\nheader:1.1\n\n\x00", [ConnectedFrame(headers={"header": "1.1"})]),  # type: ignore[typeddict-item]
        # empty header :
        (
            b"CONNECTED\nhead:\nheader:1.1\n\n\x00",
            [ConnectedFrame(headers={"head": "", "header": "1.1"})],  # type: ignore[typeddict-item]
        ),
        # header value with :
        (b"CONNECTED\nheader:what:?\n\n\x00", [ConnectedFrame(headers={})]),  # type: ignore[typeddict-item]
        # no NULL
        (b"CONNECTED\nheader:what:?\n\nhello", []),
        # header never end
        (b"CONNECTED\nheader:hello", []),
        (b"CONNECTED\nheader:hello\n", []),
        (b"CONNECTED\nheader:hello\n\x00", []),
        (b"CONNECTED\nn", []),
        # unknown command
        (b"SOME_COMMAND\nhead:\nheader:1.1\n\n\x00", [HeartbeatFrame()]),
        # unknown command
        (
            b"whatever\nWHATEVER\nheader:1.1\n\n\x00CONNECTED\nheader:1.1\n\n\x00\nwhatever\nCONNECTED\nheader:1.2\n\n\x00",
            [
                HeartbeatFrame(),
                ConnectedFrame(headers={"header": "1.1"}),  # type: ignore[typeddict-item]
                HeartbeatFrame(),
                ConnectedFrame(headers={"header": "1.2"}),  # type: ignore[typeddict-item]
            ],
        ),
        # Correct content-length with body containing NULL byte
        (
            b"MESSAGE\ncontent-length:5\n\nBod\x00y\x00",
            [MessageFrame(headers={"content-length": "5"}, body=b"Bod\x00y")],  # type: ignore[typeddict-item]
        ),
        # Content-length shorter than actual body (should only read up to content-length)
        (
            b"MESSAGE\ncontent-length:4\n\nBody\x00 with extra\x00\n",
            [MessageFrame(headers={"content-length": "4"}, body=b"Body"), HeartbeatFrame()],  # type: ignore[typeddict-item]
        ),
        # Content-length longer than actual body (should wait for more data)
        (
            b"MESSAGE\ncontent-length:10\n\nShort",
            [],
        ),
        # Content-length longer than actual body, then more data comes with NULL terminator
        (
            b"MESSAGE\ncontent-length:10\n\nShortMOREDATA\x00",
            [MessageFrame(headers={"content-length": "10"}, body=b"ShortMORED")],  # type: ignore[typeddict-item]
        ),
    ],
)
def test_load_frames(raw_frames: bytes, loaded_frames: list[AnyServerFrame]) -> None:
    assert list(FrameParser().parse_frames_from_chunk(raw_frames)) == loaded_frames
