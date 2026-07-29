# Test Strategy

The canonical test seams and domain vocabulary are defined in `CONTEXT.md`.
Tests are grouped by the interface they exercise rather than by implementation
class:

- `tests/service`: reservation behavior, invariants, provider routing, failover,
  and circuit-breaker policy.
- `tests/controller`: HTTP validation, status codes, idempotency, and response
  contracts.
- `tests/repository`: PostgreSQL concurrency, migrations, queries, and external
  provider contracts.

Every implementation slice starts with one failing behavioral test. Internal
collaborators are not mocked; only true system boundaries such as external
providers, time, and randomness receive test adapters.
