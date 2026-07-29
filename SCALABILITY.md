# Scalability

## Scope and Workload Model

No traffic volume, catalog size, provider count, or latency SLO was supplied.
This document therefore does not invent a maximum QPS. It describes the
dimensions that drive capacity, the first constraints in the current design,
and the evidence that would justify each scaling change.

The important workload dimensions are:

- checkout creation rate and items per reservation;
- concentration of demand on the same product/provider inventory row;
- percentage of allocations served by external providers;
- provider latency, error rate, and rate limits;
- confirmation/cancellation rate relative to creation rate;
- number of reservations expiring rather than completing;
- unknown provider outcomes and their retry backlog;
- retention period for reservations, allocations, and provider operations.

The write cost of a checkout grows roughly linearly with its item count. Each
item requires provider selection, a hold, and an allocation row. External
provider attempts are currently sequential because the service stops as soon
as one item's outcome is unknown. Confirmation and release likewise touch each
allocation. The most meaningful capacity tests must therefore include item
count and provider latency, not only single-item request throughput.

## How the Current Design Scales

### API processes

FastAPI processes are horizontally replicable. Request state is not stored in
memory, and correctness is enforced in PostgreSQL and through deterministic
idempotency keys. A load balancer can distribute requests without sticky
sessions.

The provider registry and circuit-breaker state are process-local. This means
replicas remain independent and no Redis dependency is needed for correctness.
It also means adding API replicas multiplies the number of calls a failing
provider may receive before every local breaker opens.

### PostgreSQL

Internal stock uses one guarded `UPDATE` rather than a read followed by a
write. Concurrent holds cannot make `reserved` exceed `on_hand`. Confirm and
cancel lock the reservation row, so conflicting lifecycle transitions
serialize cleanly. These choices scale well while contention is spread across
many products.

Indexes support the dominant operational access paths:

- reservation expiration by `(status, expires_at)`;
- provider retries by `(status, next_attempt_at)`;
- provider selection by `(product_id, allocation_priority)`;
- user reservation history by `(user_id, created_at)`.

PostgreSQL remains one consistency boundary for internal inventory, reservation
state, and order creation. That removes distributed coordination from the
normal path and is the right starting point for this workload.

### Background workers

Expiration and reconciliation are independent processes with configurable
batch size and polling interval. `FOR UPDATE SKIP LOCKED` lets multiple worker
instances claim different reservations without a coordinator. Full batches
are drained immediately; workers sleep only after a short batch or failure.

Processing is at-least-once. Unique constraints, state checks, and provider
idempotency keys make repeated work safe. A worker crash releases PostgreSQL
locks, allowing another instance to continue.

## Where It Breaks First

The first likely bottleneck is not FastAPI. It is the combination of external
provider latency and open database transactions.

### 1. Remote calls inside database transactions

Create, confirm, release, expiration, and reconciliation can wait for an HTTP
provider while a PostgreSQL transaction—and in some flows row locks—remains
open. As provider p95/p99 latency rises, transaction duration, lock wait time,
connection occupancy, and deadlock risk rise with it. Adding API replicas at
that point can make the database less available by filling the connection
pool faster.

This is the first architectural break point because it couples two unrelated
capacity domains: provider latency consumes database concurrency.

### 2. Hot-product write contention

All internal holds for one product/provider pair update the same
`inventory_levels` row. The guarded update prevents overselling, but PostgreSQL
must serialize those writes. Overall traffic may be moderate while a flash-sale
SKU experiences high lock wait and low throughput.

No cache removes this contention safely: a cache cannot make multiple writers
authoritative for the same units of stock. The solution must change ownership
or admission, not merely move the counter.

### 3. Database connections, WAL, and table churn

Every lifecycle step writes several related rows and updates timestamps.
Increasing API and worker replicas increases connection demand and WAL volume.
`provider_operations` is append/update heavy; reservation and allocation
tables continually grow. Autovacuum lag, index bloat, and pool saturation will
eventually dominate before CPU in the Python process.

### 4. Sequential provider latency and provider limits

A reservation with several externally fulfilled items accumulates provider
latency. A slow provider can dominate checkout response time. The current
circuit breaker limits repeated failures but there is no per-provider
concurrency bulkhead or rate limiter, so one provider can consume a large
share of outbound connections.

### 5. Recovery backlog

An outage can create a burst of expiring reservations and unknown operations.
Workers scale horizontally, but retrying too aggressively may delay provider
recovery or violate rate limits. Operations that exhaust their retry limit
remain unknown for manual investigation; without an operator workflow, that
backlog becomes an operational rather than computational bottleneck.

## Database Growth and Contention

The existing schema favors correctness and query locality:

- UUIDv7 keys avoid the random B-tree insertion pattern of UUIDv4.
- Narrow uniqueness constraints enforce idempotency and one-order-per-
  reservation independently of application races.
- Queue-like worker queries are indexed and bounded.
- Foreign keys keep lifecycle records consistent.

The first database changes should be operational, not structural:

1. measure slow queries, lock waits, pool checkout time, WAL rate, dead tuples,
   and autovacuum progress;
2. tune the application pool against the database connection budget;
3. use PgBouncer if connection establishment or idle client connections become
   material;
4. archive terminal reservations and provider operations according to an
   explicit audit-retention policy;
5. validate indexes with real query plans and remove unused indexes that add
   write cost.

Read replicas can serve eventually consistent history or reporting queries,
but the create/confirm/cancel paths and immediate read-after-write status
should remain on the primary. Sending correctness-sensitive reads to a lagging
replica could show stale reservation state and encourage unsafe retries.

Partitioning is justified only when retention and vacuum behavior on
`provider_operations` or terminal reservations becomes a measured problem.
Time partitioning can simplify archival, but it complicates global uniqueness
and foreign keys. It should not be the first response to table growth.

For very large append-heavy operational tables, internal `BIGINT` keys would
reduce index size compared with UUID, while public reservation IDs could remain
UUIDv7. This is an evidence-driven storage optimization, not an API change.

## Worker Throughput and Backlog

For a worker type, approximate drain capacity is:

```text
drain rate ≈ worker replicas × batch size / observed batch duration
```

This is only useful when the database and providers can sustain that
concurrency. The key SLO is backlog age, not loop count:

- **expiration lag:** `now - oldest due pending reservation.expires_at`;
- **reconciliation lag:** `now - oldest due unknown operation.next_attempt_at`;
- **exhausted backlog:** count and age of unknown operations at the attempt
  limit.

When lag grows but database and provider saturation remain low, add worker
replicas or increase batch size. When lag grows with lock waits, pool
saturation, or provider throttling, more workers will worsen the incident.
Reduce concurrency, isolate the affected provider, or increase retry delay
instead.

`SKIP LOCKED` avoids workers waiting on the same rows, but a slow remote call
still keeps claimed rows locked for the duration of the batch transaction.
The long-term design should claim durable work in a short transaction, perform
I/O outside the lock, and finalize in another short transaction.

At larger replica counts, polling intervals should include jitter to avoid
synchronized empty queries. Separate worker deployments already allow
expiration and reconciliation resources to be tuned independently.

## External Provider Isolation

Providers differ in latency, reliability, capabilities, and rate limits, so
they should not share one undifferentiated concurrency budget.

The current design already provides:

- capability-aware routing;
- deterministic allocation priority;
- per-process circuit breakers;
- explicit timeouts;
- no blind failover after an unknown outcome;
- durable confirmation/release retry with exponential backoff.

The next isolation controls should be added per provider:

- a bounded concurrency semaphore or dedicated connection pool;
- a rate limiter aligned with the provider contract;
- separate latency, error, unknown-outcome, and breaker-state metrics;
- a retry budget and maximum backoff;
- an operator-visible disabled/degraded state.

If provider traffic becomes a significant fraction of checkout latency, the
architecture should persist an operation intent and dispatch it asynchronously
through an outbox-backed worker or workflow engine. A message broker becomes
useful at that point for delivery and provider isolation. It does not replace
the database state machine or idempotency; messages can still be delivered
more than once.

## Scaling Plan

### Stage 0: current take-home scale

Run one PostgreSQL primary, one or more stateless API processes, one expiration
worker, and one reconciliation worker. Establish dashboards and load-test the
real provider mix. Vertical database scaling is the simplest first response
while data fits comfortably on one primary.

### Stage 1: independent horizontal scaling

- Add API replicas based on API concurrency and CPU, within the database
  connection budget.
- Add expiration or reconciliation replicas based on backlog age.
- Tune batch size, polling interval, HTTP connection limits, and provider
  timeouts from measurements.
- Add PgBouncer if connection count rather than query work is limiting.

This stage requires no change to domain behavior.

### Stage 2: decouple provider I/O from transactions

When provider latency causes long transactions or pool saturation:

1. persist a provider-operation intent and reservation intermediate state;
2. commit the short local transaction;
3. dispatch by provider through an outbox or durable workflow;
4. perform the remote operation with the deterministic key;
5. finalize the reservation in another short locked transaction.

This also creates the right place for per-provider bulkheads, rate limits,
compensation, and unknown-hold reconciliation. Client contracts must explicitly
accept an asynchronous `reserving`/`confirming` result.

### Stage 3: split ownership, not transactions

If one PostgreSQL writer is no longer sufficient, partition the domain by an
ownership key that prevents two writers from selling the same stock—for
example warehouse/provider or a stable product shard. Each inventory unit must
have one authoritative writer. Cross-shard reservations then require a saga
and compensation.

Multi-primary replication of the same stock counter is not a safe shortcut.
For multi-region deployments, route writes to the inventory owner's region and
use local replicas only for non-authoritative reads.

## Metrics and Decision Triggers

The current workers expose Prometheus counters, batch-duration histograms,
reservation outcome counters, and last-success timestamps. That is enough to
verify that loops are alive, but not enough to plan capacity.

Before increasing scale, add:

| Area | Measurements | Decision enabled |
| --- | --- | --- |
| API | request rate, p50/p95/p99 latency, errors by operation | API replica count and SLO health |
| Database | transaction duration, query latency, lock waits, deadlocks, pool usage | pool tuning, query work, I/O decoupling |
| Inventory | hold attempts, guarded-update misses, contention by product | distinguish real out-of-stock from hot-row pressure |
| Provider | latency, status/outcome, timeouts, breaker state, in-flight calls | timeout, bulkhead, routing, and retry budgets |
| Expiration | due count, oldest-due age, batch duration | worker count and batch size |
| Reconciliation | due/unknown/exhausted count and age, attempts | retry policy and incident response |
| Storage | table/index size, dead tuples, WAL, autovacuum lag | retention, archival, and partitioning |

Example triggers are directional rather than arbitrary constants:

- If API latency rises while database and provider latency remain flat, scale
  API compute.
- If pool wait rises with provider latency, decouple provider I/O before adding
  connections.
- If expiration lag rises with spare database capacity, add expiration
  workers.
- If reconciliation lag is isolated to one provider, apply that provider's
  bulkhead or retry budget rather than scaling every worker.
- If hot-row lock time dominates for a small SKU set, shard ownership or add an
  admission queue for those SKUs; do not cache the authoritative counter.
- If terminal data dominates active indexes and vacuum work, archive or
  partition according to retention policy.

## Simplicity Trade-offs

| Current decision | Why it is reasonable now | When it stops being right |
| --- | --- | --- |
| One PostgreSQL consistency boundary | Strong local invariants and simple failure recovery | Writer, WAL, or hot-row contention exceeds one primary |
| Synchronous provider calls | Create returns a conclusive hold result and the flow is easy to inspect | Provider latency consumes DB connections or violates checkout SLO |
| PostgreSQL-backed workers | Durable state already exists; `SKIP LOCKED` gives simple parallelism | Provider-specific isolation or event fan-out requires independent delivery |
| No inventory cache | Avoids authorizing holds from stale data | Read-only availability traffic dominates; cache may then serve hints only |
| Per-process circuit breaker | No shared dependency and failures stay isolated | Many replicas multiply calls beyond provider limits |
| One provider per item | Clear allocation and compensation semantics | Large orders routinely require split fulfillment |
| UUIDv7 primary keys | Distributed ID generation with acceptable index locality | Hot operational indexes become materially larger than a numeric alternative |

These choices favor simplicity, but not indiscriminately. Avoiding a queue and
cache is correct while PostgreSQL can own the workflow and provider calls fit
the latency budget. Keeping remote I/O inside transactions is acceptable for a
demonstration, but it is the first choice that should change under meaningful
external-provider traffic.

## Capacity and Reliability Limits

The current implementation should not be described as infinitely horizontally
scalable or production-certified. Its explicit limits are:

- no measured throughput or latency envelope yet;
- one authoritative PostgreSQL writer;
- remote calls can hold database transactions and row locks open;
- hot inventory rows serialize writes;
- no per-provider concurrency or rate-limit isolation;
- process-local circuit state;
- no durable compensation for a successful early external hold when a later
  item in the same reservation fails;
- unknown initial hold outcomes are not reconciled through
  `provider_operations`;
- exhausted retries require manual database/operational investigation;
- disaster recovery, backup restore objectives, and multi-region failover are
  deployment concerns not implemented here.

The order of remediation matters. Instrument and load-test first, decouple
remote I/O second, isolate providers third, and shard inventory ownership only
after a single PostgreSQL writer is proven insufficient. This sequence
preserves the current correctness model for as long as it remains the simpler
and safer design.
