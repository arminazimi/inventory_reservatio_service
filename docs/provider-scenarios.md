# External Provider Scenarios

The implementation deliberately covers all four suggested scenarios, not only
the minimum two. Together they prove the external HTTP contract, safe routing,
stale-data handling, and recovery from a mid-lifecycle partial failure.

## Why these four

| Scenario | Expected result | Why it matters |
| --- | --- | --- |
| Hold succeeds | Availability is fresh, the provider creates a hold, and the reservation is `pending` | Proves the integration contract and happy-path state transition actually work |
| Hold endpoint fails | A definite `5xx` contributes to the hold circuit and routing continues only inside the selected offer's `routing_group` | Proves resilience without silently changing to an unrelated marketplace seller |
| Availability is stale | The stale zero value is treated as advisory; the provider's atomic hold remains authoritative | Prevents false stock rejection caused by delayed or cached provider data |
| Confirmation times out | The reservation stays `confirming`; an idempotent provider operation is persisted for reconciliation | A timeout may occur after the provider committed, so reporting success or failure would both be unsafe |

The first two prove basic usefulness and availability. The last two are the
more important correctness cases: they force the design to distinguish a
snapshot from an atomic command and a definite failure from an unknown result.

## Routing and safety rules

1. The customer selects `(product_id, provider_id)` at checkout.
2. If that offer has no `routing_group`, only the selected provider is called.
3. If it has a group, only offers for the same product and group are candidates,
   ordered by `allocation_priority`.
4. Fresh insufficient availability, definite out-of-stock, server errors, and
   an open circuit may advance to the next grouped candidate.
5. Stale availability does not reject stock; the atomic hold is attempted.
6. A hold timeout is `unknown` and never falls back, because doing so could
   create holds at two providers.
7. Confirmation/release unknown outcomes are durable and retried with the same
   idempotency key by the reconciliation worker.

Circuit state is per provider operation. With the Compose defaults, three hold
failures open the hold circuit for 30 seconds; a successful availability read
does not reset it.

## Bruno runbook

Start a clean stack:

```bash
docker compose down --volumes
docker compose up -d --build --wait
```

Stop any separately launched host `uvicorn` on port 8000 first. The mock URL
uses Docker's `external-provider-mock` service name and therefore the checkout
request must be served by the Compose API container.

Open `bruno/inventory-reservation`, select the `local` environment, and run one
of these self-contained top-level folders:

- `00-general-flow`
- `01-provider-hold-success`
- `02-provider-failure-circuit-fallback`
- `03-stale-availability`
- `04-confirmation-failure`

Each external scenario creates its own product, provider configuration, and
offers, then resets the mock provider's call counters immediately before the
behavior under test. No shared setup folder is required. The collection is
designed for an empty business database; clean the business tables before
re-running the same folder.
