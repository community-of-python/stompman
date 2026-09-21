integration_ignores := "--ignore=packages/stompman/test_stompman/test_integration.py --ignore=packages/faststream-stomp/test_faststream_stomp/test_integration.py"

default: install lint check-types test

install:
    uv lock --upgrade
    uv sync --all-extras --all-packages --frozen

lint:
    uv run ruff check .
    uv run ruff format .

check-types:
    uv run mypy .

test-fast *args:
    uv run pytest {{integration_ignores}} {{args}}

# Resolve the declared dependency floors instead of the newest releases `install` locks to.
test-lowest *args:
    uv lock --upgrade --resolution lowest-direct
    uv sync --all-extras --all-packages --frozen
    uv run --no-sync pytest {{integration_ignores}} --no-cov {{args}}

test *args:
    #!/bin/bash
    set -euo pipefail
    trap 'echo; docker compose logs && docker compose down --remove-orphans' EXIT
    docker compose up -d
    uv run python scripts/wait_for_stomp_brokers.py
    uv run pytest {{args}}

run-artemis:
    #!/bin/bash
    trap 'echo; docker compose down --remove-orphans' EXIT
    docker compose run --service-ports activemq-artemis

run-consumer:
    uv run examples/consumer.py

run-producer:
    uv run examples/producer.py

publish package:
    rm -rf dist
    uv build --package {{package}}
    uv publish --token $PYPI_TOKEN
