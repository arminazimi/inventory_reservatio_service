# Inventory Reservation Service

A production-minded inventory reservation service built with Python, FastAPI,
PostgreSQL, Domain-Driven Design, Clean Architecture, and test-driven
development.

## Status

The project is under active implementation. Architecture, local-development,
testing, and demo instructions will be documented as each vertical slice is
completed.

## Requirements

- Python 3.14
- [uv](https://docs.astral.sh/uv/)
- PostgreSQL 17
- Docker and Docker Compose for the complete local environment

## Dependency setup

```bash
uv sync --extra dev
```

For pip-based environments:

```bash
python -m pip install -r requirements-dev.txt
```

## Local PostgreSQL

Start PostgreSQL and wait for its healthcheck:

```bash
docker compose up -d --wait postgres
```

Run the database connectivity test:

```bash
uv run pytest tests/repository/test_database.py
```

Apply the latest database schema:

```bash
uv run alembic upgrade head
```

Verify that SQLAlchemy metadata and migrations have not drifted:

```bash
uv run alembic check
```

Run the reservation expiration worker in a separate process:

```bash
uv run reservation-expiration-worker
```

The worker handles `SIGINT` and `SIGTERM` gracefully. Its batch size and polling
interval can be configured with `EXPIRATION_BATCH_SIZE` and
`EXPIRATION_POLL_INTERVAL_SECONDS`. It emits structured JSON logs and exposes
Prometheus metrics at `http://localhost:9101/metrics`. The metrics address can
be changed with `EXPIRATION_METRICS_HOST` and `EXPIRATION_METRICS_PORT`.

Run unknown provider outcome reconciliation as an independent process:

```bash
uv run reservation-reconciliation-worker
```

The reconciliation worker retries unknown confirm and release operations with
their original idempotency keys. It uses a persisted exponential backoff, a
bounded attempt count, `FOR UPDATE SKIP LOCKED` batching, graceful shutdown,
structured JSON logs, and Prometheus metrics at
`http://localhost:9102/metrics`.

Its behavior is configurable with `RECONCILIATION_BATCH_SIZE`,
`RECONCILIATION_MAX_ATTEMPTS`, `RECONCILIATION_POLL_INTERVAL_SECONDS`, and
`RECONCILIATION_RETRY_BASE_DELAY_SECONDS`. The metrics listener can be changed
with `RECONCILIATION_METRICS_HOST` and `RECONCILIATION_METRICS_PORT`.

Stop the local services without deleting database data:

```bash
docker compose down
```

## Architecture

The service uses a direct three-layer request flow:

```text
inventory_reservation/
├── controller/  # FastAPI routes and transport contracts
├── service/     # Business rules and workflow orchestration
└── repository/  # PostgreSQL and external-provider access
```

Dependencies flow from `controller` to `service` to `repository`. See
[`CONTEXT.md`](CONTEXT.md) for the ubiquitous language and test seams, and
[`docs/adr/0001-three-layer-architecture.md`](docs/adr/0001-three-layer-architecture.md)
for the architectural decision.

The tables, constraints, and operational indexes are described in
[`docs/database-schema.md`](docs/database-schema.md).
