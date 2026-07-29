from dataclasses import dataclass
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict

from inventory_reservation.service.provider import (
    CircuitBreakerProvider,
    ConfirmCommand,
    HoldCommand,
    InventoryProvider,
    ProviderCallFailedError,
    ProviderConfirmAttempt,
    ProviderHoldAttempt,
)


class _HoldResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hold_reference: str


class HttpInventoryProvider:
    """Adapt the external provider hold HTTP contract to the domain port."""

    def __init__(
        self,
        *,
        provider_id: UUID,
        base_url: str,
        timeout: float,
        client: httpx.AsyncClient,
    ) -> None:
        self.provider_id = provider_id
        self._hold_url = f"{base_url.rstrip('/')}/holds"
        self._timeout = timeout
        self._client = client

    async def hold(self, command: HoldCommand) -> ProviderHoldAttempt:
        try:
            response = await self._client.post(
                self._hold_url,
                headers={"Idempotency-Key": command.idempotency_key},
                json={
                    "product_id": str(command.product_id),
                    "quantity": command.quantity,
                },
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            return ProviderHoldAttempt.unknown()

        if response.status_code == httpx.codes.CONFLICT:
            return ProviderHoldAttempt.out_of_stock()
        if response.is_server_error:
            raise ProviderCallFailedError(self.provider_id)

        response.raise_for_status()
        payload = _HoldResponse.model_validate(response.json())
        return ProviderHoldAttempt.held(reference=payload.hold_reference)

    async def confirm(self, command: ConfirmCommand) -> ProviderConfirmAttempt:
        try:
            response = await self._client.post(
                f"{self._hold_url}/{command.hold_reference}/confirm",
                headers={"Idempotency-Key": command.idempotency_key},
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            return ProviderConfirmAttempt.unknown()

        if response.status_code in {
            httpx.codes.NOT_FOUND,
            httpx.codes.CONFLICT,
        }:
            return ProviderConfirmAttempt.rejected()
        if response.is_server_error:
            raise ProviderCallFailedError(self.provider_id)

        response.raise_for_status()
        return ProviderConfirmAttempt.confirmed()


@dataclass(frozen=True, slots=True)
class _ExternalProviderSettings:
    base_url: str
    timeout: float


class ProviderRegistry:
    """Keep external provider adapters and their circuit state across transactions."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
    ) -> None:
        self._client = client
        self._failure_threshold = failure_threshold
        self._recovery_timeout = recovery_timeout
        self._providers: dict[
            UUID,
            tuple[_ExternalProviderSettings, InventoryProvider],
        ] = {}

    def get_external(
        self,
        *,
        provider_id: UUID,
        base_url: str,
        timeout: float,
    ) -> InventoryProvider:
        settings = _ExternalProviderSettings(
            base_url=base_url,
            timeout=timeout,
        )
        registered = self._providers.get(provider_id)
        if registered is not None and registered[0] == settings:
            return registered[1]

        provider = CircuitBreakerProvider(
            provider=HttpInventoryProvider(
                provider_id=provider_id,
                base_url=base_url,
                timeout=timeout,
                client=self._client,
            ),
            failure_threshold=self._failure_threshold,
            recovery_timeout=self._recovery_timeout,
        )
        self._providers[provider_id] = (settings, provider)
        return provider
