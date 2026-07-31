# Inventory Reservation Bruno Collection

This collection exercises a complete marketplace checkout against a clean
local database. Requests are intentionally numbered and must run in order.

## Open and run

1. Start the stack with `docker compose up -d --build --wait`.
2. In Bruno, choose **Open Collection** and select this directory.
3. Select the `local` environment.
4. Run the entire collection, or run exactly the folder you want to test.

Do not keep a separate host `uvicorn` on `127.0.0.1:8000` while running the
Compose scenarios. External provider URLs use Docker service DNS and must be
handled by the API container. Stop the host process first, or override
`baseUrl` with the actual published Compose address.

Every top-level folder is self-contained. Response scripts store generated IDs
as runtime variables, so no manual copying is required and no external setup
folder must be run first.

The collection has exactly five top-level flows:

- `00-general-flow`: the complete internal marketplace checkout, confirmation,
  cancellation, idempotency, and selected-seller failure flow.
- `01-provider-hold-success`: availability and remote hold succeed.
- `02-provider-failure-circuit-fallback`: definite provider failure, grouped
  fallback, and an open hold circuit.
- `03-stale-availability`: a stale zero snapshot remains advisory and the
  authoritative hold succeeds.
- `04-confirmation-failure`: confirmation times out and remains durably
  `confirming` for reconciliation.

Each external scenario creates its own product, providers, and offers before
resetting the mock counters and exercising the behavior. The failure scenario
assumes the Compose default circuit threshold of three and proves the fourth
reservation skips the broken hold endpoint while still using its grouped
fallback.

The collection is designed for an empty business database. Re-running the same
folder without cleaning its records will intentionally hit product/provider
uniqueness constraints. The rationale and expected transitions are documented
in `docs/provider-scenarios.md`.
