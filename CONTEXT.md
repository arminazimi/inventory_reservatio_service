# Project Context

## Purpose

The Inventory Reservation Service protects stock during checkout. It creates
temporary reservations, confirms them after successful payment, and releases
them after cancellation, payment failure, or expiration.

The service coordinates inventory owned by the platform and inventory exposed
by external providers. Correctness, consistency, explicit failure handling, and
operational visibility take priority over feature count.

## Ubiquitous Language

- **Product**: a sellable item identified by a stable product ID and SKU.
- **Inventory Provider**: the system or owner responsible for a source of stock.
- **Product Offer**: one product offered by one provider, including the
  provider-owned stock state needed by this bounded context. The pair
  `(product_id, provider_id)` is its identity here.
- **Routing Group**: an explicit set of operationally interchangeable offers.
  Offers outside the selected offer's group are never candidates for fallback.
- **Reservation**: a temporary claim created for a user's checkout.
- **Reservation Item**: the requested quantity of one product from the
  provider explicitly selected at checkout.
- **Allocation**: the part of a reservation item assigned to a provider.
- **Hold**: a provider-confirmed temporary claim on stock.
- **Confirmation**: consumption of held stock after payment succeeds.
- **Release**: return of held stock after cancellation or expiration.
- **Order**: the durable result created after every allocation is confirmed.
- **Provider Operation**: an idempotent hold, confirm, or release attempt.
- **Unknown Outcome**: a remote operation that may have succeeded even though no
  conclusive response was received.

## Bounded Context

This repository contains one bounded context: **Inventory Reservation**.
Product catalog, user authentication, cart management, and payment processing
are external concerns. The service accepts a verified `user_id` and a payment
outcome rather than implementing those capabilities.

## Confirmed Test Seams

Tests observe behavior through these interfaces:

1. `ReservationService` for create, confirm, cancel, and retrieve behavior;
   `ReservationExpirationWorker` for expiration batching, polling, and graceful
   shutdown; `ReservationReconciliationWorker` for explicitly resolving
   unknown provider outcomes.
2. `ProviderRouter` for provider capability checks, failure classification,
   unknown outcomes, and circuit-breaker behavior inside the service layer.
3. The controller HTTP interface for request validation, status codes,
   idempotency, and response contracts.
4. Repository interfaces for PostgreSQL transaction, constraint, migration,
   concurrency, and provider-client behavior.

Tests do not target private methods or ORM internals. External provider APIs,
time, and randomness may be replaced at their explicit seams.

## Initial Assumptions

- A reservation has a finite TTL.
- A reservation is successful only when every requested item is allocated to
  its selected provider.
- The catalog/product-detail context presents seller offers and sends the
  selected `(product_id, provider_id)` pair to this service. Price, warranty,
  shipping promise, and seller presentation remain catalog concerns.
- A read-only external provider is not eligible for a hard reservation unless
  the platform owns an authoritative allocation quota for it.
- Checkout uses exactly the customer-selected provider unless its offer has a
  `routing_group`. Grouped offers are an explicit business assertion that the
  providers are interchangeable; definite out-of-stock or temporary failure
  may route to the next group member by allocation priority.
- An ungrouped marketplace seller is never silently replaced by another
  seller. A timeout/unknown hold outcome never falls back, even inside a
  routing group, because the first provider may already have created a hold.
- A timeout is an unknown outcome, not proof of failure.
- Provider operations use deterministic idempotency keys.
- One reservation can create at most one order.
