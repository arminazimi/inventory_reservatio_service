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
