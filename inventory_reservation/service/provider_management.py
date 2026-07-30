from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID, uuid7

SENSITIVE_PUBLIC_CONFIG_KEYS = frozenset(
    {
        "api_key",
        "access_token",
        "authorization",
        "client_secret",
        "password",
        "refresh_token",
        "secret",
        "token",
    }
)


class ProviderKind(StrEnum):
    INTERNAL = "internal"
    EXTERNAL = "external"


class ProviderAuthType(StrEnum):
    NONE = "none"
    API_KEY = "api_key"
    BEARER = "bearer"
    BASIC = "basic"
    OAUTH2 = "oauth2"


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


@dataclass(frozen=True, slots=True)
class UpdateProviderCommand:
    name: str | None = None
    base_url: str | None = None
    request_timeout_ms: int | None = None
    capabilities: ProviderCapabilities | None = None


@dataclass(frozen=True, slots=True)
class SetProviderCredentialsCommand:
    auth_type: ProviderAuthType
    secret_ref: str | None
    public_config: dict[str, object]


@dataclass(frozen=True, slots=True)
class ProviderCredentialConfiguration:
    provider_id: UUID
    auth_type: ProviderAuthType
    secret_ref: str | None
    public_config: dict[str, object]


class ProviderRepositoryPort(Protocol):
    async def list(self) -> tuple[ProviderConfiguration, ...]: ...

    async def get(
        self,
        provider_id: UUID,
    ) -> ProviderConfiguration | None: ...

    async def add(self, provider: ProviderConfiguration) -> None: ...

    async def update(self, provider: ProviderConfiguration) -> bool: ...

    async def set_enabled(
        self,
        provider_id: UUID,
        *,
        is_enabled: bool,
    ) -> ProviderConfiguration | None: ...

    async def upsert_credentials(
        self,
        credentials: ProviderCredentialConfiguration,
    ) -> ProviderCredentialConfiguration: ...

    async def get_credentials(
        self,
        provider_id: UUID,
    ) -> ProviderCredentialConfiguration | None: ...


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
        _validate_provider_configuration(provider)
        await self._repository.add(provider)
        return provider

    async def update_provider(
        self,
        provider_id: UUID,
        command: UpdateProviderCommand,
    ) -> ProviderConfiguration:
        if all(
            value is None
            for value in (
                command.name,
                command.base_url,
                command.request_timeout_ms,
                command.capabilities,
            )
        ):
            raise InvalidProviderConfigurationError(
                "Provider update must include at least one mutable field."
            )

        current = await self.get_provider(provider_id)
        updated = replace(
            current,
            name=command.name if command.name is not None else current.name,
            base_url=(
                command.base_url
                if command.base_url is not None
                else current.base_url
            ),
            request_timeout_ms=(
                command.request_timeout_ms
                if command.request_timeout_ms is not None
                else current.request_timeout_ms
            ),
            capabilities=(
                command.capabilities
                if command.capabilities is not None
                else current.capabilities
            ),
        )
        _validate_provider_configuration(updated)
        if not await self._repository.update(updated):
            raise ProviderNotFoundError(provider_id)
        return updated

    async def enable_provider(
        self,
        provider_id: UUID,
    ) -> ProviderConfiguration:
        provider = await self.get_provider(provider_id)
        if provider.is_enabled:
            return provider

        _validate_provider_configuration(provider)
        enabled_provider = await self._repository.set_enabled(
            provider_id,
            is_enabled=True,
        )
        if enabled_provider is None:
            raise ProviderNotFoundError(provider_id)
        return enabled_provider

    async def disable_provider(
        self,
        provider_id: UUID,
    ) -> ProviderConfiguration:
        provider = await self.get_provider(provider_id)
        if not provider.is_enabled:
            return provider

        disabled_provider = await self._repository.set_enabled(
            provider_id,
            is_enabled=False,
        )
        if disabled_provider is None:
            raise ProviderNotFoundError(provider_id)
        return disabled_provider

    async def set_provider_credentials(
        self,
        provider_id: UUID,
        command: SetProviderCredentialsCommand,
    ) -> ProviderCredentialConfiguration:
        provider = await self.get_provider(provider_id)
        if (
            provider.kind is ProviderKind.INTERNAL
            and command.auth_type is not ProviderAuthType.NONE
        ):
            raise InvalidProviderConfigurationError(
                "Internal provider does not support external credentials."
            )
        if (
            command.auth_type is not ProviderAuthType.NONE
            and (
                command.secret_ref is None
                or not command.secret_ref.strip()
            )
        ):
            raise InvalidProviderConfigurationError(
                "Authenticated provider requires a non-blank secret reference."
            )
        if (
            command.secret_ref is not None
            and len(command.secret_ref) > 255
        ):
            raise InvalidProviderConfigurationError(
                "Secret reference must not exceed 255 characters."
            )
        if (
            command.auth_type is ProviderAuthType.NONE
            and command.secret_ref is not None
        ):
            raise InvalidProviderConfigurationError(
                "Unauthenticated provider must not define a secret reference."
            )
        if _contains_sensitive_public_config(command.public_config):
            raise InvalidProviderConfigurationError(
                "Public credential config must not contain secret values."
            )
        if command.auth_type is ProviderAuthType.BASIC:
            username = command.public_config.get("username")
            if (
                not isinstance(username, str)
                or not username.strip()
                or ":" in username
            ):
                raise InvalidProviderConfigurationError(
                    "Basic authentication requires a non-blank public username."
                )
        header_name = command.public_config.get("header_name")
        if header_name is not None:
            if not isinstance(header_name, str) or not header_name.strip():
                raise InvalidProviderConfigurationError(
                    "Credential header name must be a non-blank string."
                )
            if header_name.casefold() == "idempotency-key":
                raise InvalidProviderConfigurationError(
                    "Credential header must not overwrite Idempotency-Key."
                )
        credentials = ProviderCredentialConfiguration(
            provider_id=provider_id,
            auth_type=command.auth_type,
            secret_ref=command.secret_ref,
            public_config=command.public_config,
        )
        return await self._repository.upsert_credentials(credentials)


def _contains_sensitive_public_config(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, nested_value in value.items():
            normalized_key = str(key).strip().lower().replace("-", "_")
            if normalized_key in SENSITIVE_PUBLIC_CONFIG_KEYS:
                return True
            if _contains_sensitive_public_config(nested_value):
                return True
        return False
    if isinstance(value, list):
        return any(
            _contains_sensitive_public_config(item)
            for item in value
        )
    return False


def _validate_provider_configuration(
    provider: ProviderConfiguration,
) -> None:
    if not provider.name.strip():
        raise InvalidProviderConfigurationError(
            "Provider name must not be blank."
        )
    if provider.request_timeout_ms <= 0:
        raise InvalidProviderConfigurationError(
            "Provider request timeout must be positive."
        )
    if (
        provider.kind is ProviderKind.INTERNAL
        and provider.base_url is not None
    ):
        raise InvalidProviderConfigurationError(
            "Internal provider must not define a base URL."
        )
    if (
        provider.kind is ProviderKind.INTERNAL
        and provider.driver != "internal"
    ):
        raise InvalidProviderConfigurationError(
            "Internal provider driver must be 'internal'."
        )
    if provider.kind is ProviderKind.EXTERNAL and not provider.base_url:
        raise InvalidProviderConfigurationError(
            "External provider requires a base URL."
        )
    if (
        provider.kind is ProviderKind.EXTERNAL
        and provider.driver != "http"
    ):
        raise InvalidProviderConfigurationError(
            "External provider driver must be 'http'."
        )
    if provider.kind is ProviderKind.EXTERNAL:
        parsed_base_url = urlsplit(provider.base_url)
        if (
            parsed_base_url.scheme not in {"http", "https"}
            or not parsed_base_url.netloc
        ):
            raise InvalidProviderConfigurationError(
                "External provider base URL must use HTTP or HTTPS."
            )
    if provider.capabilities.hold and not (
        provider.capabilities.confirm
        and provider.capabilities.release
    ):
        raise InvalidProviderConfigurationError(
            "Hold-capable provider must support confirm and release."
        )
