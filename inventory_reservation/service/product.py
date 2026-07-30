from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID, uuid7


@dataclass(frozen=True, slots=True)
class Product:
    id: UUID
    sku: str
    name: str
    is_active: bool


@dataclass(frozen=True, slots=True)
class CreateProductCommand:
    sku: str
    name: str


class ProductRepositoryPort(Protocol):
    async def add(self, product: Product) -> None: ...

    async def get(self, product_id: UUID) -> Product | None: ...


class InvalidProductConfigurationError(ValueError):
    pass


class ProductSkuConflictError(ValueError):
    def __init__(self, sku: str) -> None:
        self.sku = sku
        super().__init__(f"Product SKU {sku!r} is already in use")


class ProductManagementService:
    def __init__(
        self,
        *,
        repository: ProductRepositoryPort,
        product_id_factory: Callable[[], UUID] = uuid7,
    ) -> None:
        self._repository = repository
        self._product_id_factory = product_id_factory

    async def create_product(
        self,
        command: CreateProductCommand,
    ) -> Product:
        sku = command.sku.strip()
        if not sku:
            raise InvalidProductConfigurationError(
                "Product SKU must not be blank."
            )
        if len(sku) > 100:
            raise InvalidProductConfigurationError(
                "Product SKU must not exceed 100 characters."
            )
        name = command.name.strip()
        if not name:
            raise InvalidProductConfigurationError(
                "Product name must not be blank."
            )
        if len(name) > 255:
            raise InvalidProductConfigurationError(
                "Product name must not exceed 255 characters."
            )
        product = Product(
            id=self._product_id_factory(),
            sku=sku,
            name=name,
            is_active=True,
        )
        await self._repository.add(product)
        return product
