from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid7


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


@dataclass(frozen=True, slots=True)
class RegisterProviderCommand:
    name: str
    kind: ProviderKind
    driver: str
    base_url: str | None
    request_timeout_ms: int
    capabilities: ProviderCapabilities


class ProviderRepositoryPort(Protocol):
    async def list(self) -> tuple[ProviderConfiguration, ...]: ...

    async def get(
        self,
        provider_id: UUID,
    ) -> ProviderConfiguration | None: ...

    async def add(self, provider: ProviderConfiguration) -> None: ...


class ProviderNotFoundError(LookupError):
    def __init__(self, provider_id: UUID) -> None:
        self.provider_id = provider_id
        super().__init__(f"Provider {provider_id} was not found")


class InvalidProviderConfigurationError(ValueError):
    pass


class ProviderNameConflictError(ValueError):
    def __init__(self, provider_name: str) -> None:
        self.provider_name = provider_name
        super().__init__(f"Provider name {provider_name!r} is already in use")


class ProviderManagementService:
    def __init__(
        self,
        *,
        repository: ProviderRepositoryPort,
        provider_id_factory: Callable[[], UUID] = uuid7,
    ) -> None:
        self._repository = repository
        self._provider_id_factory = provider_id_factory

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

    async def register_provider(
        self,
        command: RegisterProviderCommand,
    ) -> ProviderConfiguration:
        if not command.name.strip():
            raise InvalidProviderConfigurationError(
                "Provider name must not be blank."
            )
        if command.request_timeout_ms <= 0:
            raise InvalidProviderConfigurationError(
                "Provider request timeout must be positive."
            )
        if (
            command.kind is ProviderKind.EXTERNAL
            and not command.base_url
        ):
            raise InvalidProviderConfigurationError(
                "External provider requires a base URL."
            )
        if (
            command.kind is ProviderKind.EXTERNAL
            and command.driver != "http"
        ):
            raise InvalidProviderConfigurationError(
                "External provider driver must be 'http'."
            )
        if command.kind is ProviderKind.EXTERNAL:
            parsed_base_url = urlsplit(command.base_url)
            if (
                parsed_base_url.scheme not in {"http", "https"}
                or not parsed_base_url.netloc
            ):
                raise InvalidProviderConfigurationError(
                    "External provider base URL must use HTTP or HTTPS."
                )
        if command.capabilities.hold and not (
            command.capabilities.confirm
            and command.capabilities.release
        ):
            raise InvalidProviderConfigurationError(
                "Hold-capable provider must support confirm and release."
            )

        provider = ProviderConfiguration(
            id=self._provider_id_factory(),
            name=command.name,
            kind=command.kind,
            driver=command.driver,
            base_url=command.base_url,
            request_timeout_ms=command.request_timeout_ms,
            capabilities=command.capabilities,
            is_enabled=False,
        )
        await self._repository.add(provider)
        return provider
