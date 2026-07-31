import os
from base64 import b64encode
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from inventory_reservation.service.provider import (
    AvailabilityCommand,
    CircuitBreakerProvider,
    ConfirmCommand,
    HoldCommand,
    InventoryProvider,
    ProviderAvailabilityAttempt,
    ProviderCallFailedError,
    ProviderConfirmAttempt,
    ProviderHoldAttempt,
    ProviderReleaseAttempt,
    ReleaseCommand,
    SecretResolutionError,
    SecretResolver,
)
from inventory_reservation.service.provider_management import (
    ProviderAuthType,
    ProviderCredentialConfiguration,
)


class _HoldResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    hold_reference: str


class _AvailabilityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available_quantity: int = Field(ge=0)
    observed_at: datetime


def _utc_now() -> datetime:
    return datetime.now(UTC)


class EnvironmentSecretResolver:
    """Resolve env:// references without exposing secret values to configuration."""

    def __init__(
        self,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._environ = os.environ if environ is None else environ

    async def resolve(self, secret_ref: str) -> str:
        scheme, separator, variable_name = secret_ref.partition("://")
        if (
            separator != "://"
            or scheme != "env"
            or not variable_name
        ):
            raise SecretResolutionError
        secret = self._environ.get(variable_name)
        if not secret:
            raise SecretResolutionError
        return secret


@dataclass(frozen=True, slots=True)
class ProviderAuthentication:
    credentials: ProviderCredentialConfiguration
    secret_resolver: SecretResolver | None


class HttpInventoryProvider:
    """Adapt the external provider hold HTTP contract to the domain port."""

    def __init__(  # noqa: PLR0913
        self,
        *,
        provider_id: UUID,
        base_url: str,
        timeout: float,
        client: httpx.AsyncClient,
        authentication: ProviderAuthentication | None = None,
        availability_max_age: timedelta = timedelta(seconds=60),
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.provider_id = provider_id
        self._hold_url = f"{base_url.rstrip('/')}/holds"
        self._timeout = timeout
        self._client = client
        self._authentication = authentication
        self._availability_max_age = availability_max_age
        self._clock = clock

    async def availability(
        self,
        command: AvailabilityCommand,
    ) -> ProviderAvailabilityAttempt:
        try:
            response = await self._client.get(
                f"{self._hold_url.removesuffix('/holds')}/availability/{command.product_id}",
                headers=await self._request_headers(
                    f"availability:product:{command.product_id}"
                ),
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            return ProviderAvailabilityAttempt.temporarily_unavailable()

        if response.is_server_error:
            raise ProviderCallFailedError(self.provider_id)
        response.raise_for_status()
        try:
            payload = _AvailabilityResponse.model_validate(response.json())
        except (ValidationError, ValueError):
            raise ProviderCallFailedError(self.provider_id) from None
        if payload.observed_at.tzinfo is None:
            raise ProviderCallFailedError(self.provider_id)
        if self._clock() - payload.observed_at > self._availability_max_age:
            return ProviderAvailabilityAttempt.stale(
                available_quantity=payload.available_quantity,
                observed_at=payload.observed_at,
            )
        return ProviderAvailabilityAttempt.fresh(
            available_quantity=payload.available_quantity,
            observed_at=payload.observed_at,
        )

    async def hold(self, command: HoldCommand) -> ProviderHoldAttempt:
        try:
            response = await self._client.post(
                self._hold_url,
                headers=await self._request_headers(command.idempotency_key),
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

    async def _request_headers(
        self,
        idempotency_key: str,
    ) -> dict[str, str]:
        headers = {"Idempotency-Key": idempotency_key}
        authentication = self._authentication
        credentials = (
            authentication.credentials
            if authentication is not None
            else None
        )
        if (
            credentials is None
            or credentials.auth_type is ProviderAuthType.NONE
        ):
            return headers
        if (
            authentication is None
            or authentication.secret_resolver is None
            or credentials.secret_ref is None
        ):
            raise ProviderCallFailedError(self.provider_id)

        try:
            secret = await authentication.secret_resolver.resolve(
                credentials.secret_ref
            )
        except SecretResolutionError:
            raise ProviderCallFailedError(self.provider_id) from None
        header_name = credentials.public_config.get(
            "header_name",
            (
                "X-API-Key"
                if credentials.auth_type is ProviderAuthType.API_KEY
                else "Authorization"
            ),
        )
        if (
            not isinstance(header_name, str)
            or not header_name.strip()
            or header_name.casefold() == "idempotency-key"
        ):
            raise ProviderCallFailedError(self.provider_id)

        if credentials.auth_type is ProviderAuthType.API_KEY:
            headers[header_name] = secret
            return headers
        if credentials.auth_type is ProviderAuthType.BASIC:
            username = credentials.public_config.get("username")
            if (
                not isinstance(username, str)
                or not username.strip()
                or ":" in username
            ):
                raise ProviderCallFailedError(self.provider_id)
            encoded_credentials = b64encode(
                f"{username}:{secret}".encode()
            ).decode()
            headers[header_name] = f"Basic {encoded_credentials}"
            return headers
        if credentials.auth_type not in {
            ProviderAuthType.BEARER,
            ProviderAuthType.OAUTH2,
        }:
            raise ProviderCallFailedError(self.provider_id)

        scheme = credentials.public_config.get("scheme", "Bearer")
        if not isinstance(scheme, str):
            raise ProviderCallFailedError(self.provider_id)
        headers[header_name] = f"{scheme} {secret}"
        return headers

    async def confirm(self, command: ConfirmCommand) -> ProviderConfirmAttempt:
        try:
            response = await self._client.post(
                f"{self._hold_url}/{command.hold_reference}/confirm",
                headers=await self._request_headers(
                    command.idempotency_key
                ),
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

    async def release(self, command: ReleaseCommand) -> ProviderReleaseAttempt:
        try:
            response = await self._client.post(
                f"{self._hold_url}/{command.hold_reference}/release",
                headers=await self._request_headers(
                    command.idempotency_key
                ),
                timeout=self._timeout,
            )
        except httpx.TimeoutException:
            return ProviderReleaseAttempt.unknown()

        if response.status_code == httpx.codes.NOT_FOUND:
            return ProviderReleaseAttempt.released()
        if response.is_server_error:
            raise ProviderCallFailedError(self.provider_id)

        response.raise_for_status()
        return ProviderReleaseAttempt.released()


@dataclass(frozen=True, slots=True)
class _ExternalProviderSettings:
    base_url: str
    timeout: float
    credentials: ProviderCredentialConfiguration | None


class ProviderRegistry:
    """Keep external provider adapters and their circuit state across transactions."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        secret_resolver: SecretResolver | None = None,
        failure_threshold: int = 3,
        recovery_timeout: float = 30.0,
    ) -> None:
        self._client = client
        self._secret_resolver = secret_resolver
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
        credentials: ProviderCredentialConfiguration | None = None,
    ) -> InventoryProvider:
        settings = _ExternalProviderSettings(
            base_url=base_url,
            timeout=timeout,
            credentials=credentials,
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
                authentication=(
                    ProviderAuthentication(
                        credentials=credentials,
                        secret_resolver=self._secret_resolver,
                    )
                    if credentials is not None
                    else None
                ),
            ),
            failure_threshold=self._failure_threshold,
            recovery_timeout=self._recovery_timeout,
        )
        self._providers[provider_id] = (settings, provider)
        return provider
