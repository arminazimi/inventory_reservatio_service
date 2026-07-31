# Inventory Reservation Service

A production-minded checkout inventory service built with Python 3.14,
FastAPI, PostgreSQL, SQLAlchemy, Alembic, and Prometheus.

The service temporarily reserves stock while payment is in progress, confirms
the reservation after successful payment, and releases it after cancellation
or expiry. Each checkout line identifies the seller/provider selected on the
product page, including the platform's own internal offer.

## What is implemented

- Atomic, concurrency-safe internal inventory holds.
- Idempotent reservation creation, confirmation, and cancellation.
- Multi-item reservation with compensating release on partial failure.
- Explicit product-offer selection by `(product_id, provider_id)`.
- Internal inventory and HTTP-based external providers.
- Explicit `routing_group` fallback without cross-seller substitution.
- Fresh/stale external availability classification and snapshot refresh.
- Timeout handling as an unknown outcome, preventing unsafe double holds.
- Per-provider circuit breakers with a single half-open recovery probe.
- Durable reconciliation of unknown confirm and release operations.
- Automatic expiry with horizontally safe `FOR UPDATE SKIP LOCKED` workers.
- Product, provider, credential-reference, and inventory management APIs.
- One-order-per-confirmed-reservation database invariant.
- Liveness, database readiness, Prometheus metrics, and structured worker logs.
- Multi-stage non-root Docker image, Docker Compose stack, and GitHub Actions CI.

The project uses Domain-Driven Design language and Clean Architecture
dependency inversion within a pragmatic three-layer design. The runtime call
flow is:

```text
HTTP / worker controller
          ↓
domain service and use-case orchestration
          ↓
PostgreSQL and external-provider repositories
```

The service layer has no FastAPI, SQLAlchemy, or HTTPX dependency. It defines
the ports implemented by infrastructure adapters.

## Quick start with Docker

Requirements: a recent Docker Desktop or Docker Engine with Compose v2
(`--wait` support) and BuildKit.

```bash
cp .env.example .env
docker compose up -d --build --wait
```

This starts PostgreSQL, applies Alembic migrations, and runs the API plus both
background workers.

| Process | Address | Purpose |
| --- | --- | --- |
| API | `http://localhost:8000` | Checkout and management APIs |
| OpenAPI | `http://localhost:8000/docs` | Interactive API documentation |
| API metrics | `http://localhost:8000/metrics` | HTTP request and latency metrics |
| Expiration metrics | `http://localhost:9101/metrics` | Expiry worker metrics |
| Reconciliation metrics | `http://localhost:9102/metrics` | Recovery worker metrics |
| Provider mock | `http://localhost:9000` | Deterministic external-provider scenarios |

Compose waits for the API and both workers to become healthy. Inspect all
processes:

```bash
docker compose ps
```

Verify API-process liveness and its PostgreSQL dependency:

```bash
curl --fail http://localhost:8000/health/live
curl --fail http://localhost:8000/health/ready
```

Expected responses:

```json
{"status":"ok"}
{"status":"ready","checks":{"database":"up"}}
```

Stop the stack while retaining PostgreSQL data:

```bash
docker compose down
```

To remove the local database volume as well:

```bash
docker compose down --volumes
```

## Checkout demo

Run the stack first, then execute the following commands from the repository
root. This is the happy-path checkout demonstration: it creates a product,
configures an internal provider with five units, reserves two units, confirms
the reservation, and verifies that three units remain. Provider faults,
cancellation, expiry, and reconciliation are covered by the automated tests.

```bash
DEMO_SUFFIX=$(date +%s)
DEMO_USER_ID=0191f4b8-7d4a-7000-8000-000000000001
```

Create a product:

```bash
PRODUCT_ID=$(
  curl --fail --silent --show-error \
    --request POST http://localhost:8000/internal/v1/products \
    --header 'Content-Type: application/json' \
    --data "{\"sku\":\"DEMO-${DEMO_SUFFIX}\",\"name\":\"Demo product\"}" |
  docker compose exec -T api \
    python -c 'import json, sys; print(json.load(sys.stdin)["id"])'
)
echo "product: ${PRODUCT_ID}"
```

Register and enable an internal provider:

```bash
PROVIDER_ID=$(
  curl --fail --silent --show-error \
    --request POST http://localhost:8000/internal/v1/providers \
    --header 'Content-Type: application/json' \
    --data "{
      \"name\":\"demo-internal-${DEMO_SUFFIX}\",
      \"kind\":\"internal\",
      \"driver\":\"internal\",
      \"request_timeout_ms\":500,
      \"capabilities\":{
        \"availability\":true,
        \"hold\":true,
        \"confirm\":true,
        \"release\":true
      }
    }" |
  docker compose exec -T api \
    python -c 'import json, sys; print(json.load(sys.stdin)["id"])'
)

curl --fail --silent --show-error \
  --request POST \
  "http://localhost:8000/internal/v1/providers/${PROVIDER_ID}/enable" |
docker compose exec -T api python -m json.tool
```

Assign five units of inventory:

```bash
INVENTORY_URL="http://localhost:8000/internal/v1/products/${PRODUCT_ID}/providers/${PROVIDER_ID}/inventory"

curl --fail --silent --show-error \
  --request PUT "${INVENTORY_URL}" \
  --header 'Content-Type: application/json' \
  --data '{"on_hand":5,"allocation_priority":10}' |
docker compose exec -T api python -m json.tool
```

Create a reservation. Repeating this request with the same body and
`Idempotency-Key` returns the same reservation.

```bash
RESERVATION_JSON=$(
  curl --fail --silent --show-error \
    --request POST http://localhost:8000/v1/reservations \
    --header "X-User-ID: ${DEMO_USER_ID}" \
    --header "Idempotency-Key: demo-checkout-${DEMO_SUFFIX}" \
    --header 'Content-Type: application/json' \
    --data "{
      \"items\":[{
        \"product_id\":\"${PRODUCT_ID}\",
        \"provider_id\":\"${PROVIDER_ID}\",
        \"quantity\":2
      }]
    }"
)
echo "${RESERVATION_JSON}" |
docker compose exec -T api python -m json.tool

RESERVATION_ID=$(
  echo "${RESERVATION_JSON}" |
  docker compose exec -T api \
    python -c 'import json, sys; print(json.load(sys.stdin)["id"])'
)
```

Confirm the reservation. Repeating confirmation is also safe.

```bash
curl --fail --silent --show-error \
  --request POST \
  "http://localhost:8000/v1/reservations/${RESERVATION_ID}/confirm" \
  --header "X-User-ID: ${DEMO_USER_ID}" |
docker compose exec -T api python -m json.tool
```

Verify the final inventory:

```bash
curl --fail --silent --show-error "${INVENTORY_URL}" |
docker compose exec -T api python -m json.tool
```

The final inventory response contains:

```json
{
  "on_hand": 3,
  "reserved": 0,
  "available": 3
}
```

After completing the development setup below, the automated equivalent is:

```bash
uv run pytest tests/e2e/test_checkout.py -vv
```

That test runs the FastAPI application in-process against the configured
PostgreSQL database and removes the records it creates. It does not call the
already-running Compose API.

## Bruno collection

A native Bruno collection is available in
[`bruno/inventory-reservation`](bruno/inventory-reservation). Select its
`local` environment and run the numbered folders in order against an empty
business database. Response scripts carry generated product, provider, and
reservation IDs between requests automatically.

The collection has one self-contained general marketplace flow and four
self-contained external-provider scenario folders. Each folder creates its own
setup, so it can be run without executing another folder first. See
[`docs/provider-scenarios.md`](docs/provider-scenarios.md) for the expected
state transitions and the reason each scenario was selected.

## Provider behavior

Each `product_offers` row associates one product with one provider. The caller
selects that offer on the product page and sends both IDs in the reservation
item. For an external provider, the row establishes the offer association; the
remote hold response remains authoritative for availability. A hold-capable
external provider uses this contract:

- `GET /availability/{product_id}`
- `POST /holds`
- `POST /holds/{hold_reference}/confirm`
- `POST /holds/{hold_reference}/release`

Every operation receives a deterministic `Idempotency-Key`. A fresh
availability response can skip a provider that conclusively lacks quantity;
a stale response is advisory and the atomic hold remains authoritative.

Fallback is opt-in. With no `routing_group`, the selected seller is the only
candidate because price, warranty, and delivery terms can differ. When offers
share a routing group, definite out-of-stock, server failure, or an open hold
circuit advances to the next member by `allocation_priority`. A timeout is an
unknown outcome, so routing stops: the provider may have accepted the hold
before the response was lost and trying another provider could double-hold.

Circuit state is isolated by provider operation. For example, a healthy
availability endpoint cannot close a failing hold circuit. The default
threshold is three failures, followed by a 30-second open interval and one
half-open probe.

Unknown confirmation and release outcomes are persisted in `confirming` or
`releasing` state and retried by the reconciliation worker. An unknown initial
hold is deliberately not retried or failed over, but it is not yet durably
reconciled; closing that crash/timeout gap requires a persisted hold intent and
a provider status-query contract.

Provider secrets are never stored directly. Credential configuration contains
an `env://VARIABLE_NAME` reference resolved at call time:

```json
{
  "auth_type": "bearer",
  "secret_ref": "env://ACME_PROVIDER_TOKEN",
  "public_config": {
    "header_name": "Authorization",
    "scheme": "Bearer"
  }
}
```

The referenced secret must be available to every runtime that may call the
provider—the API and both workers. A host export or Compose `.env` entry alone
does not inject an arbitrary variable into a container. For a local external
provider demo, create an uncommitted `compose.provider-secrets.yaml`:

```yaml
services:
  api:
    environment:
      ACME_PROVIDER_TOKEN: ${ACME_PROVIDER_TOKEN:?set ACME_PROVIDER_TOKEN}
  expiration-worker:
    environment:
      ACME_PROVIDER_TOKEN: ${ACME_PROVIDER_TOKEN:?set ACME_PROVIDER_TOKEN}
  reconciliation-worker:
    environment:
      ACME_PROVIDER_TOKEN: ${ACME_PROVIDER_TOKEN:?set ACME_PROVIDER_TOKEN}
```

Then export the secret and apply the override:

```bash
export ACME_PROVIDER_TOKEN='replace-with-runtime-secret'
docker compose \
  --file compose.yaml \
  --file compose.provider-secrets.yaml \
  up -d --build --wait
```

Use a provider-specific variable name and do not commit secret-bearing
configuration. Production deployments should replace the environment resolver
with Vault or a cloud secret manager.

API key, Bearer, Basic, pre-resolved OAuth2 bearer-token, and unauthenticated
HTTP header configurations are supported. OAuth2 token acquisition and
refresh are outside the current adapter.

## Development setup

Requirements:

- Python 3.14
- [uv](https://docs.astral.sh/uv/)
- PostgreSQL 17, normally via Docker

Install the locked development environment:

```bash
uv sync --extra dev
docker compose up -d --wait postgres
uv run alembic upgrade head
```

Copying `.env.example` to `.env` is optional; the application and Compose file
have matching local defaults. Create `.env` only when overriding them.

Run the API locally:

```bash
uv run uvicorn inventory_reservation.controller.main:app --reload
```

Run workers in separate terminals when needed:

```bash
uv run reservation-expiration-worker
uv run reservation-reconciliation-worker
```

Configuration defaults are documented in `.env.example`. Important controls
include reservation TTL, provider circuit-breaker thresholds, worker batch
sizes, polling intervals, retry limits, and metrics ports.

## Tests and quality gates

The test suite is organized around confirmed public seams rather than private
implementation details:

- `tests/service`: domain behavior, provider policies, circuit breaking, and
  worker policy.
- `tests/controller`: HTTP validation, status codes, and response contracts.
- `tests/repository`: PostgreSQL concurrency, migrations, and external HTTP
  provider contracts.
- `tests/e2e`: complete HTTP checkout backed by PostgreSQL.

After installing dependencies and starting PostgreSQL, run the same core
checks used in CI:

```bash
docker compose up -d --wait postgres
uv run ruff check .
uv run mypy inventory_reservation
uv run alembic upgrade head
uv run alembic check
uv run pytest --cov=inventory_reservation --cov-report=term-missing
docker compose config --quiet
docker build --check .
```

Coverage is branch-aware and enforced at a minimum of 85%. The suite includes
concurrent hold tests, idempotency races, selected-provider failure cases,
multi-item compensation, reconciliation backoff, worker behavior, and a full
HTTP checkout.

## Repository guide

```text
inventory_reservation/
├── controller/  FastAPI routes, composition roots, and worker entry points
├── service/     Domain models, ports, policies, and use cases
└── repository/  PostgreSQL and external-provider adapters

migrations/      Alembic migration history
tests/           Controller, service, repository, and E2E tests
docs/            Schema documentation and architectural decisions
```

Read these documents in order:

1. [`CONTEXT.md`](CONTEXT.md) — bounded context, ubiquitous language, and test
   seams.
2. [`ARCHITECTURE.md`](ARCHITECTURE.md) — lifecycle, consistency model,
   provider failure semantics, and trade-offs.
3. [`SCALABILITY.md`](SCALABILITY.md) — bottlenecks, metrics, and staged
   scaling plan.
4. [`docs/database-schema.md`](docs/database-schema.md) — tables, constraints,
   and operational indexes.
5. [`docs/adr/0001-three-layer-architecture.md`](docs/adr/0001-three-layer-architecture.md)
   — why the project uses a direct three-layer structure.

## Deliberate boundaries

- Authentication, cart, product catalog ownership, and payment execution are
  outside this bounded context. `X-User-ID` and payment outcomes are assumed
  to come from trusted upstream services.
- Management endpoints are intentionally unauthenticated for the take-home
  environment and must be protected by an internal gateway or service identity
  before production use.
- PostgreSQL is the source of truth for reservations, orders, provider
  operation state, and internal inventory; cache is not used to authorize
  internal holds. An external provider remains authoritative for stock it
  owns.
- External calls currently occur inside database transactions. The first
  scale-driven redesign would persist an operation intent and execute remote
  I/O outside the transaction.
- A single provider must satisfy an item; split fulfillment is not implemented.
- The catalog owns offer presentation and commercial attributes. This bounded
  context uses `(product_id, provider_id)` as the compact offer identity rather
  than duplicating seller, price, warranty, or shipping tables.

The deeper reasoning and production evolution plan are intentionally kept in
`ARCHITECTURE.md` and `SCALABILITY.md` so this README stays executable and
reviewer-focused.
