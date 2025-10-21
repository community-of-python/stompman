# stompman Project Context

## Project Overview

stompman is a modern, asynchronous Python client for the STOMP (Simple Text Oriented Messaging Protocol) messaging protocol. It provides a typed, modern, and comprehensible API for working with STOMP-compatible message brokers like ActiveMQ Artemis and ActiveMQ Classic.

The project consists of two main packages:
1. `stompman` - The core STOMP client library
2. `faststream-stomp` - A STOMP broker implementation for the FastStream framework

## Key Features

- Fully asynchronous implementation using Python's asyncio
- Modern, typed API with comprehensive type hints
- Automatic connection management with reconnection capabilities
- Support for transactions, subscriptions, and message acknowledgment
- Integration with FastStream framework
- Support for both ActiveMQ Artemis and ActiveMQ Classic
- Implements STOMP 1.2 protocol specification
- Built-in heartbeat support

## Project Structure

```
stompman/
├── packages/
│   ├── stompman/              # Core STOMP client library
│   │   ├── stompman/          # Main source code
│   │   └── test_stompman/     # Unit and integration tests
│   └── faststream-stomp/      # FastStream integration
│       ├── faststream_stomp/  # Main source code
│       └── test_faststream_stomp/  # Tests
├── examples/                  # Usage examples
├── docker-compose.yml         # Development environment with ActiveMQ instances
└── Justfile                   # Task runner with common commands
```

## Core Components (stompman package)

### Main Classes

- `Client` - Main entry point for interacting with STOMP servers
- `ConnectionParameters` - Configuration for connecting to STOMP servers
- `Heartbeat` - Configuration for heartbeat intervals
- `Transaction` - Context manager for transactional message sending
- `AutoAckSubscription` - Subscription with automatic message acknowledgment
- `ManualAckSubscription` - Subscription with manual message acknowledgment

### Key Methods

- `Client.send()` - Send messages to destinations
- `Client.subscribe()` - Subscribe to destinations with auto-acknowledgment
- `Client.subscribe_with_manual_ack()` - Subscribe with manual acknowledgment
- `Client.begin()` - Start a transaction context
- `Client.is_alive()` - Check if the client connection is healthy

## FastStream Integration (faststream-stomp package)

Provides a `StompBroker` class that integrates with the FastStream framework, allowing developers to build event-driven applications with STOMP messaging.

## Development Environment

The project uses Docker Compose to provide a development environment with ActiveMQ instances:

- ActiveMQ Artemis on port 9000
- ActiveMQ Classic on port 9001

## Building and Running

### Prerequisites

- Python 3.11+
- uv (package manager)
- Docker and Docker Compose (for development environment)

### Common Commands

Using Just (task runner):

```bash
# Install dependencies
just install

# Run linting
just lint

# Check types
just check-types

# Run fast tests (excluding integration tests)
just test-fast

# Run all tests (requires Docker)
just test

# Run integration environment
just run-artemis

# Run example consumer
just run-consumer

# Run example producer
just run-producer
```

Using uv directly:

```bash
# Install dependencies
uv sync --all-extras --all-packages --frozen

# Run linting
uv run ruff check .
uv run ruff format .

# Check types
uv run mypy .

# Run tests
uv run pytest
```

### Development Workflow

1. Start the development environment: `docker compose up -d`
2. Install dependencies: `just install`
3. Run tests: `just test`
4. Make changes to the code
5. Run linting and type checking: `just lint && just check-types`

## Testing

The project uses pytest for testing with the following configuration:

- Unit tests are located in `test_stompman/` directories
- Integration tests require running ActiveMQ instances
- Tests use both ActiveMQ Artemis and ActiveMQ Classic when possible
- Code coverage reporting is enabled by default

## Coding Standards

The project follows these coding standards:

- Strict type checking with mypy
- Code formatting with ruff
- Comprehensive unit and integration tests
- Modern Python features (3.11+)
- Clean, readable, and well-documented code

## Examples

The `examples/` directory contains sample code showing how to:

- Create a basic consumer
- Create a basic producer
- Use the FastStream integration
- Implement broadcast messaging patterns
