# STOMPMAN Project Context for AI Agents

## Project Overview

**stompman** is a modern, asynchronous Python client for the STOMP (Simple Text Oriented Messaging Protocol) that provides a typed, comprehensible API. It's designed to be:

- Asynchronous (using Python's asyncio)
- Actively maintained (not abandoned)
- Type-safe with modern Python typing
- Compatible with STOMP 1.2 specification
- Tested with ActiveMQ Artemis and ActiveMQ Classic

The project follows a monorepo structure with two main packages:
1. `stompman` - The core STOMP client library
2. `faststream-stomp` - A STOMP broker implementation for FastStream

## Architecture and Key Components

### Core Modules

- `client.py` - Main `Client` class that manages connections, subscriptions, and transactions
- `frames.py` - Data classes representing STOMP protocol frames (Connect, Connected, Send, Subscribe, etc.)
- `serde.py` - Serialization/deserialization logic for STOMP frames
- `connection.py` - Low-level connection handling with abstract interface
- `connection_manager.py` - Manages connection lifecycle, retries, and failover
- `connection_lifespan.py` - Handles the connection lifecycle states
- `subscription.py` - Subscription management with auto/manual ACK modes
- `transaction.py` - Transaction support for atomic message operations
- `config.py` - Configuration data classes (ConnectionParameters, Heartbeat)
- `errors.py` - Custom exception hierarchy
- `logger.py` - Centralized logging

### Key Features

1. **Automatic Reconnection**: Handles connection failures with configurable retry logic
2. **Load Balancing**: Supports multiple server connections with automatic failover
3. **Heartbeat Management**: Automatic heartbeat sending/receiving to detect connection liveness
4. **Subscription Management**: Both auto-ACK and manual-ACK subscription modes
5. **Transaction Support**: Context manager-based transaction handling
6. **Type Safety**: Fully typed API with comprehensive type hints

## Development Workflow

### Prerequisites

- Python 3.11+
- uv (package manager)
- Docker (for integration tests)

### Setup Commands

```bash
# Install dependencies
just install

# Alternative manual setup
uv lock --upgrade
uv sync --all-extras --all-packages --frozen
```

### Development Commands

```bash
# Run linters
just lint
# Equivalent to:
# uv run ruff check .
# uv run ruff format .

# Check types
just check-types
# Equivalent to:
# uv run mypy .

# Run fast tests (unit tests only)
just test-fast

# Run all tests (including integration tests with Docker)
just test

# Run specific tests
just test-fast packages/stompman/test_stompman/test_frame_serde.py
```

### Testing

The project uses pytest with comprehensive test coverage:

- Unit tests for individual components
- Integration tests with real STOMP servers (ActiveMQ Artemis/Classic)
- Property-based testing with Hypothesis
- Test fixtures for connection parameters and asyncio backends

Integration tests require Docker containers to be running:
```bash
just test
```

### Code Quality

- Strict mypy type checking enabled
- Ruff linting with comprehensive ruleset
- 100% test coverage enforced
- Modern Python features (3.11+) required

## Usage Patterns

### Basic Client Usage

```python
async with stompman.Client(
    servers=[
        stompman.ConnectionParameters(host="171.0.0.1", port=61616, login="user1", passcode="passcode1"),
    ],
    heartbeat=stompman.Heartbeat(will_send_interval_ms=1000, want_to_receive_interval_ms=1000),
) as client:
    # Send messages
    await client.send(b"hi there!", destination="DLQ", headers={"persistent": "true"})

    # Subscribe to messages
    await client.subscribe("DLQ", handle_message_from_dlq, on_suppressed_exception=print)
```

### Transaction Usage

```python
async with client.begin() as transaction:
    for _ in range(10):
        await transaction.send(body=b"hi there!", destination="DLQ", headers={"persistent": "true"})
        await asyncio.sleep(0.1)
```

### Manual ACK Subscription

```python
async def handle_message_from_dlq(message_frame: stompman.AckableMessageFrame) -> None:
    print(message_frame.body)
    await message_frame.ack()

await client.subscribe_with_manual_ack("DLQ", handle_message_from_dlq, ack="client")
```

## Project Conventions

### Code Style

- Line length: 120 characters
- Strict typing with `from __future__ import annotations`
- Dataclasses with `slots=True` for performance
- Comprehensive docstrings for public APIs
- Modern Python syntax (structural pattern matching, etc.)

### Testing Conventions

- Test files named `test_*.py`
- Use of pytest parametrization for comprehensive test cases
- Property-based testing for serialization/deserialization
- Mocking only when necessary, preferring integration tests
- Test coverage reporting enabled

### Git Workflow

- Feature branches from main
- Squash merges for clean history
- Semantic commit messages
- Pull requests with CI checks

## Common Development Tasks

### Adding New Frame Types

1. Add frame definition in `frames.py`
2. Update `COMMANDS_TO_FRAMES` and `FRAMES_TO_COMMANDS` mappings in `serde.py`
3. Add serialization/deserialization tests in `test_frame_serde.py`
4. Update exports in `__init__.py`

### Modifying Connection Logic

1. Changes usually belong in `connection.py` or `connection_manager.py`
2. Update corresponding tests in `test_connection.py` or `test_connection_manager.py`
3. Integration tests may need updates in `test_integration.py`

### Adding Configuration Options

1. Add fields to appropriate dataclass in `config.py`
2. Pass through to relevant components in `client.py`
3. Update README.md documentation
4. Add tests for new configuration behavior

## Debugging Tips

- Use `stompman.logger` for debugging connection issues
- Enable verbose logging with `logging.getLogger('stompman').setLevel(logging.DEBUG)`
- Check integration tests for real-world usage examples
- Use the examples/ directory for quick prototyping
