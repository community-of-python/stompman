default: install lint check-types test test-integration

install:
    uv lock --upgrade
    uv sync --all-extras --all-packages --frozen

lint:
    uv run ruff check .
    uv run ruff format .

check-types:
    uv run mypy .

test *args:
    uv run pytest {{args}}

test-integration *args:
    #!/bin/bash
    trap 'echo; docker compose down --remove-orphans' EXIT
    docker compose run --build --rm app .venv/bin/pytest packages/stompman/test_stompman/integration.py packages/faststream-stomp/test_faststream_stomp/integration.py --no-cov {{args}}

run-artemis:
    #!/bin/bash
    trap 'echo; docker compose down --remove-orphans' EXIT
    docker compose run --service-ports artemis

run-consumer:
    uv run examples/consumer.py

run-producer:
    uv run examples/producer.py

publish package:
    rm -rf dist
    uv build --package {{package}}
    uv publish --token $PYPI_TOKEN
