# Database Schema

PostgreSQL is the source of truth for reservations, internal stock, orders, and
durable provider work. All quantities are integer units.

Primary keys use native PostgreSQL `UUID` columns populated with application-
generated UUIDv7 values. UUIDv7 preserves globally unique, non-enumerable IDs
while improving B-tree insertion locality compared with random UUIDv4. A future
high-volume `provider_operations` table may move to an internal `BIGINT`
identity if index size becomes a measured bottleneck.

## Tables

| Table | Responsibility |
| --- | --- |
| `products` | Minimal product identity required by reservations |
| `inventory_providers` | Internal or external stock sources and capabilities |
| `provider_credentials` | Authentication method and non-secret secret reference |
| `product_offers` | Product/provider association and its stock reservation state |
| `reservations` | User checkout reservation and lifecycle state |
| `reservation_items` | Requested product quantities and checkout-selected provider |
| `inventory_allocations` | Provider selected for each reservation item |
| `provider_operations` | Durable, idempotent hold/confirm/release work |
| `orders` | Final result of a confirmed reservation |
| `order_items` | Product quantities copied into the final order |

## Core Consistency Rules

- `(product_id, provider_id)` is unique in `product_offers` and acts as the
  offer identity inside this bounded context.
- `reservation_items.provider_id` persists the provider selected by the caller;
  `inventory_allocations.provider_id` records the provider that actually held
  stock after any group-scoped routing.
- `product_offers.routing_group` is nullable. `NULL` means the selected offer
  is not substitutable; equal non-null values opt offers into the same fallback
  route for that product.
- `0 <= reserved <= on_hand` is enforced by PostgreSQL.
- `(user_id, idempotency_key)` is unique for reservations.
- A reservation cannot contain the same product twice.
- Allocation and order quantities must be positive.
- Each provider operation has a unique idempotency key.
- A reservation in `releasing` stores a constrained `release_target_status`
  (`cancelled`, `expired`, or `failed`) so reconciliation can finish the
  original transition or a compensating release after a restart.
- `orders.reservation_id` is unique, preventing duplicate orders.
- Actual provider secrets are not stored; `secret_ref` points to environment or
  secret-manager material.

## Operational Indexes

- `(reservations.status, reservations.expires_at)` supports expiry workers.
- `(provider_operations.status, provider_operations.next_attempt_at)` supports
  retry workers using `FOR UPDATE SKIP LOCKED`.
- `(product_offers.product_id, allocation_priority)` supports deterministic
  offer listing; checkout lookup uses the unique `(product_id, provider_id)`
  constraint. `(product_id, routing_group, allocation_priority)` supports the
  bounded fallback candidate query.
- `(reservations.user_id, created_at)` supports reservation history.
