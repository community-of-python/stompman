# stompman Project Context

## Project Overview

stompman is a modern, asynchronous Python client for the STOMP (Simple Text Oriented Messaging Protocol) messaging protocol. It provides a typed, modern, and comprehensible API for working with STOMP-compatible message brokers like ActiveMQ Artemis and ActiveMQ Classic.

The project consists of two main packages:
1. `stompman` - The core STOMP client library
2. `faststream-stomp` - A FastStream broker implementation for STOMP

## Key Features

- Fully asynchronous implementation using Python's asyncio
- Modern, typed API with comprehensive type hints
- Automatic connection management with reconnection capabilities
- Support for transactions, subscriptions, and message acknowledgment
- Built-in heartbeat support for connection health monitoring
- Integration with FastStream for declarative message handling
- Compatible with STOMP 1.2 protocol specification
- Tested with ActiveMQ Artemis and ActiveMQ Classic

## Project Structure

```
stompman/
├── packages/
│   ├── stompman/           # Core STOMP client library
│   │   ├── stompman/       # Main source code
│   │   └── test_stompman/  # Unit and integration tests
│   └── faststream-stomp/   # FastStream broker implementation
│       ├── faststream_stomp/  # Main source code
│       └── test_faststream_stomp/  # Unit and integration tests
├── examples/               # Usage examples
├── docker-compose.yml      # Development environment with ActiveMQ containers
└── Justfile                # Project commands and workflows
```

## Core Components (stompman package)

### Main Classes

- `Client` - The main entry point for interacting with STOMP servers
- `ConnectionParameters` - Configuration for connecting to STOMP servers
- `Heartbeat` - Configuration for connection heartbeats

### Key Methods

- `Client.send()` - Send messages to destinations
- `Client.subscribe()` - Subscribe to destinations with automatic ACK/NACK handling
- `Client.subscribe_with_manual_ack()` - Subscribe with manual ACK/NACK control
- `Client.begin()` - Start a transaction context manager
- `Client.is_alive()` - Check connection health

### Error Handling

- `FailedAllConnectAttemptsError` - Raised when all connection attempts fail
- `FailedAllWriteAttemptsError` - Raised when writes fail after all retries
- Various other specific error types for different failure scenarios

## FastStream Integration (faststream-stomp package)

Provides a FastStream broker implementation that allows using FastStream's declarative approach with STOMP:

- `StompBroker` - Main broker class
- Decorators for subscribers and publishers
- Testing utilities with `TestStompBroker`

## Development Environment

The project uses Docker Compose to provide a development environment with:
- ActiveMQ Artemis on port 9000
- ActiveMQ Classic on port 9001

## Building and Running

### Prerequisites

- Python 3.11 or newer
- uv (package manager)
- Docker and Docker Compose (for development environment)

### Setup

```bash
# Install dependencies
just install

# Or manually:
uv lock --upgrade
uv sync --all-extras --all-packages --frozen
```

### Running Tests

```bash
# Run fast tests (unit tests only)
just test-fast

# Run all tests (including integration tests with Docker)
just test

# Run tests with specific arguments
just test-fast -k "test_specific_feature"
```

### Code Quality

```bash
# Run linters
just lint

# Check types
just check-types

# Format code
uv run ruff format .
```

### Running Examples

```bash
# Start ActiveMQ Artemis
just run-artemis

# Run consumer example
just run-consumer

# Run producer example
just run-producer
```

## Development Conventions

### Code Style

- Strict adherence to type hints with mypy in strict mode
- Code formatting with ruff (line length 120)
- Comprehensive unit and integration tests
- Modern Python features (3.11+) encouraged

### Testing

- Unit tests in `test_stompman/` directory
- Integration tests that require Docker containers
- Property-based testing with hypothesis
- Test coverage reporting enabled

### CI/CD

- Automated testing on multiple platforms
- Type checking and linting in CI pipeline
- Automated publishing to PyPI

## Common Development Tasks

1. **Adding a new feature**:
   - Implement in the appropriate module under `stompman/`
   - Add unit tests in `test_stompman/`
   - Update documentation in docstrings and README if needed

2. **Fixing a bug**:
   - Write a failing test that reproduces the issue
   - Fix the implementation
   - Verify the test now passes

3. **Updating dependencies**:
   - Modify `pyproject.toml` files
   - Run `uv lock --upgrade` to update lock files

4. **Running integration tests**:
   - Ensure Docker is running
   - Run `just test` to start containers and run tests
