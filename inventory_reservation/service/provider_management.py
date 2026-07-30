from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID


class ProviderKind(StrEnum):
    INTERNAL = "internal"
    EXTERNAL = "external"


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    availability: bool
    hold: bool
    confirm: bool
    release: bool


@dataclass(frozen=True, slots=True)
class ProviderConfiguration:
    id: UUID
    name: str
    kind: ProviderKind
    driver: str
    base_url: str | None
    request_timeout_ms: int
    capabilities: ProviderCapabilities
    is_enabled: bool


class ProviderRepositoryPort(Protocol):
    async def list(self) -> tuple[ProviderConfiguration, ...]: ...

    async def get(
        self,
        provider_id: UUID,
    ) -> ProviderConfiguration | None: ...


class ProviderNotFoundError(LookupError):
    def __init__(self, provider_id: UUID) -> None:
        self.provider_id = provider_id
        super().__init__(f"Provider {provider_id} was not found")


class ProviderManagementService:
    def __init__(self, *, repository: ProviderRepositoryPort) -> None:
        self._repository = repository

    async def list_providers(self) -> tuple[ProviderConfiguration, ...]:
        return await self._repository.list()

    async def get_provider(
        self,
        provider_id: UUID,
    ) -> ProviderConfiguration:
        provider = await self._repository.get(provider_id)
        if provider is None:
            raise ProviderNotFoundError(provider_id)
        return provider
