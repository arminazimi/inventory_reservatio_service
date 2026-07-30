from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from inventory_reservation.repository.database import Database
from inventory_reservation.repository.models import ProductModel
from inventory_reservation.service.product import (
    Product,
    ProductSkuConflictError,
)

PRODUCT_SKU_CONSTRAINT = "uq_products_sku"


class SqlAlchemyProductRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def add(self, product: Product) -> None:
        try:
            async with self._database.session() as session, session.begin():
                session.add(
                    ProductModel(
                        id=product.id,
                        sku=product.sku,
                        name=product.name,
                        is_active=product.is_active,
                    )
                )
        except IntegrityError as error:
            if _violates_constraint(error, PRODUCT_SKU_CONSTRAINT):
                raise ProductSkuConflictError(product.sku) from error
            raise

    async def get(self, product_id: UUID) -> Product | None:
        statement = select(ProductModel).where(
            ProductModel.id == product_id
        )
        async with self._database.session() as session:
            product = (await session.scalars(statement)).one_or_none()

        if product is None:
            return None
        return Product(
            id=product.id,
            sku=product.sku,
            name=product.name,
            is_active=product.is_active,
        )


def _violates_constraint(
    error: IntegrityError,
    constraint_name: str,
) -> bool:
    cause: BaseException | None = error
    while cause is not None:
        if getattr(cause, "constraint_name", None) == constraint_name:
            return True
        cause = cause.__cause__
    return False
