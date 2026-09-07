# STOMP domain vocabulary

- **Runtime** owns application lifecycle and composes the protocol owners.
- **Negotiated connection** is a transport whose STOMP handshake has succeeded.
- **Session** owns one negotiated connection, reader, and permanent generation.
- **Command** owns transmission and optional receipt confirmation, including cancellation.
- **Generation** identifies a ready session; recovery advances it after restoration.
- **Restoration intent** is an active subscription or open transaction registered for recovery.
- **Subscription specification** is an immutable snapshot of delivery and restoration intent.
- **Channel** owns admission and settlement for a subscription on one session.
- **Reservation** charges capacity until both handler execution and settlement finish.
- **Settlement** is an ACK/NACK decision tied to the session that delivered the message.
- **Journal** contains immutable SEND entries eligible for transaction replay.
- **Compatibility adapter** translates mutable legacy contracts into native operations.

See [the ownership and reading map](docs/session-core.md) for the module interfaces.
