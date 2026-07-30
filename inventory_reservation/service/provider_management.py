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


class ProviderManagementService:
    def __init__(self, *, repository: ProviderRepositoryPort) -> None:
        self._repository = repository

    async def list_providers(self) -> tuple[ProviderConfiguration, ...]:
        return await self._repository.list()
