from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict

from inventory_reservation.service.provider import (
    HoldCommand,
    ProviderCallFailedError,
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
