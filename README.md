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

