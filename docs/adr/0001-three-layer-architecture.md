# ADR 0001: Three-Layer Architecture

- Status: Accepted
- Date: 2026-07-29

## Context

The service must coordinate transactional PostgreSQL inventory with remote
inventory providers. The implementation window is three days, so the code
structure must remain easy to navigate while preserving testable business
logic and explicit dependency direction.

## Decision

Use a three-layer architecture:

```text
Controller -> Service -> Repository
```

### Controller

The controller layer owns FastAPI routes, request and response schemas, header
parsing, HTTP status codes, and exception-to-response mapping. It does not
contain reservation decisions or persistence queries.

### Service

The service layer owns reservation lifecycle rules, provider selection,
failover, circuit-breaker policy, compensation, idempotency decisions, and
transaction orchestration. This is the primary behavioral test seam.

### Repository

The repository layer owns PostgreSQL access, SQLAlchemy mappings, atomic stock
updates, migrations, and external-provider HTTP clients. It does not decide
reservation state transitions or HTTP responses.

Dependencies only point in this direction:

```text
controller -> service -> repository
```

Composition code may construct concrete repositories and inject them into
services. Tests may replace database and remote-provider repositories at those
explicit seams.

## Guardrails

- A controller must not issue SQL or call a provider directly.
- A repository must not return FastAPI responses or choose HTTP status codes.
- A service must not import FastAPI.
- A layer is not created per entity; modules are organized around meaningful
  reservation behavior.
- Pass-through classes that add no policy, invariant, or isolation are avoided.

## Consequences

### Positive

- The request flow is direct and easy to explain.
- Business behavior remains independently testable.
- Database and remote-provider details stay outside controllers.
- The structure fits the delivery window without framework-heavy ceremony.

### Negative

- The service layer can become too large unless behavior is grouped carefully.
- Repository code contains different I/O mechanisms, so database and provider
  modules must remain separate inside that layer.
- Layer discipline must be enforced through tests and review.

