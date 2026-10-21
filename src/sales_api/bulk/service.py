from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum, auto

from ..catalog.model import Product, ProductVariant
from ..catalog.service import CatalogService, ProductChannelListingInput, ProductCreateInput, VariantCreateInput
from ..common.errors import DomainError, DomainException, ErrorCodes, ErrorCollector, ensure
from ..inventory.service import InventoryService, StockInput
from ..permissions.model import Permission, Principal
from ..persistence.store import Store
from ..shop_context import ShopContext, require_permission


class BulkErrorPolicy(StrEnum):
    ATOMIC = auto()
    PARTIAL = auto()


@dataclass(frozen=True)
class BulkRowResult[TOut]:
    index: int
    result: TOut | None
    errors: tuple[DomainError, ...]

    @property
    def succeeded(self) -> bool:
        return not self.errors and self.result is not None


@dataclass(frozen=True)
class BulkResult[TOut]:
    rows: tuple[BulkRowResult[TOut], ...]

    @property
    def count(self) -> int:
        return sum(1 for r in self.rows if r.succeeded)

    @property
    def has_errors(self) -> bool:
        return any(r.errors for r in self.rows)


class BulkService:
    MAX_ROWS = 100

    def __init__(self, ctx: ShopContext, catalog: CatalogService, inventory: InventoryService):
        self._ctx = ctx
        self._catalog = catalog
        self._inventory = inventory

    def create_variants(
        self, principal: Principal, rows: Sequence[VariantCreateInput], policy: BulkErrorPolicy
    ) -> BulkResult[ProductVariant]:
        require_permission(principal, Permission.MANAGE_PRODUCTS)
        _ensure_size(len(rows))

        def run(store: Store) -> BulkResult[ProductVariant]:
            # If a SKU repeats within the batch, all rows with it are rejected.
            sku_counts: dict[str, int] = {}
            for r in rows:
                if r.sku is not None:
                    key = r.sku.strip().upper()
                    sku_counts[key] = sku_counts.get(key, 0) + 1

            errors = []
            for row in rows:
                row_errors = list(self._catalog.validate_variant(store, row))
                if row.sku is not None and sku_counts[row.sku.strip().upper()] > 1:
                    row_errors.append(
                        DomainError(ErrorCodes.DUPLICATE, "sku", f"SKU '{row.sku}' is duplicated in the batch.")
                    )
                errors.append(row_errors)

            def compensate(variant: ProductVariant) -> None:
                for warehouse_id in store.warehouses:
                    store.stocks.pop((warehouse_id, variant.id), None)
                store.variants.remove(variant.id)

            return _apply(
                rows, errors, policy, lambda row: self._catalog.create_variant_tx(principal, store, row), compensate
            )

        return self._ctx.db.write(run)

    def create_products(
        self,
        principal: Principal,
        rows: Sequence[tuple[ProductCreateInput, Sequence[ProductChannelListingInput]]],
        policy: BulkErrorPolicy,
    ) -> BulkResult[Product]:
        require_permission(principal, Permission.MANAGE_PRODUCTS)
        _ensure_size(len(rows))

        def run(store: Store) -> BulkResult[Product]:
            errors = []
            for product_input, listings in rows:
                e = ErrorCollector()
                if product_input.product_type_id not in store.product_types:
                    e.add(ErrorCodes.NOT_FOUND, "productType", "Product type not found.")
                if not product_input.name or not product_input.name.strip():
                    e.add(ErrorCodes.REQUIRED, "name", "Name is required.")
                if any(listing.is_published for listing in listings) and product_input.category_id is None:
                    e.add(
                        ErrorCodes.CATEGORY_MISSING,
                        "channelListings",
                        "You must select a category to be able to publish.",
                    )
                errors.append(e.errors)

            def create(row: tuple[ProductCreateInput, Sequence[ProductChannelListingInput]]) -> Product:
                product_input, listings = row
                product = self._catalog.create_product_tx(principal, store, product_input)
                if listings:
                    self._catalog.update_product_channel_listings_tx(principal, store, product.id, listings, None)
                return product

            return _apply(
                rows,
                errors,
                policy,
                create,
                lambda product: self._catalog.delete_product_tx(principal, store, product.id),
            )

        return self._ctx.db.write(run)

    def update_stocks(
        self, principal: Principal, rows: Sequence[StockInput], policy: BulkErrorPolicy
    ) -> BulkResult[StockInput]:
        require_permission(principal, Permission.MANAGE_PRODUCTS)
        _ensure_size(len(rows))

        def run(store: Store) -> BulkResult[StockInput]:
            errors = []
            for r in rows:
                e = ErrorCollector()
                if r.variant_id not in store.variants:
                    e.add(ErrorCodes.NOT_FOUND, "variantId", "Variant not found.")
                if r.warehouse_id not in store.warehouses:
                    e.add(ErrorCodes.NOT_FOUND, "warehouseId", "Warehouse not found.")
                if r.quantity < 0:
                    e.add(ErrorCodes.INVALID, "quantity", "Quantity cannot be negative.")
                errors.append(e.errors)

            def update(row: StockInput) -> StockInput:
                self._inventory.set_stock(store, row)
                return row

            return _apply(rows, errors, policy, update)

        return self._ctx.db.write(run)

    def delete_products(
        self, principal: Principal, ids: Sequence[uuid.UUID], policy: BulkErrorPolicy
    ) -> BulkResult[uuid.UUID]:
        require_permission(principal, Permission.MANAGE_PRODUCTS)
        _ensure_size(len(ids))

        def run(store: Store) -> BulkResult[uuid.UUID]:
            errors = [
                [] if id_ in store.products else [DomainError(ErrorCodes.NOT_FOUND, "id", f"Product {id_} not found.")]
                for id_ in ids
            ]

            def delete(id_: uuid.UUID) -> uuid.UUID:
                self._catalog.delete_product_tx(principal, store, id_)
                return id_

            return _apply(ids, errors, policy, delete)

        return self._ctx.db.write(run)

    def update_publication(
        self,
        principal: Principal,
        rows: Sequence[tuple[uuid.UUID, ProductChannelListingInput]],
        policy: BulkErrorPolicy,
    ) -> BulkResult[uuid.UUID]:
        require_permission(principal, Permission.MANAGE_PRODUCTS)
        _ensure_size(len(rows))

        def run(store: Store) -> BulkResult[uuid.UUID]:
            errors = []
            for product_id, listing in rows:
                e = ErrorCollector()
                p = store.products.get(product_id)
                if p is None:
                    e.add(ErrorCodes.NOT_FOUND, "productId", "Product not found.")
                elif listing.is_published and p.category_id is None:
                    e.add(
                        ErrorCodes.CATEGORY_MISSING,
                        "isPublished",
                        "You must select a category to be able to publish.",
                    )
                if listing.channel_id not in store.channels:
                    e.add(ErrorCodes.NOT_FOUND, "channelId", "Channel not found.")
                errors.append(e.errors)

            def update(row: tuple[uuid.UUID, ProductChannelListingInput]) -> uuid.UUID:
                product_id, listing = row
                self._catalog.update_product_channel_listings_tx(principal, store, product_id, [listing], None)
                return product_id

            return _apply(rows, errors, policy, update)

        return self._ctx.db.write(run)


def _apply[TIn, TOut](
    rows: Sequence[TIn],
    errors: Sequence[Sequence[DomainError]],
    policy: BulkErrorPolicy,
    apply: Callable[[TIn], TOut],
    compensate: Callable[[TOut], None] | None = None,
) -> BulkResult[TOut]:
    results: list[BulkRowResult[TOut]] = []
    any_errors = any(errors)
    for i, row in enumerate(rows):
        if errors[i] or (any_errors and policy == BulkErrorPolicy.ATOMIC):
            results.append(BulkRowResult(i, None, tuple(errors[i])))
            continue

        try:
            results.append(BulkRowResult(i, apply(row), ()))
        except DomainException as ex:
            if policy == BulkErrorPolicy.PARTIAL:
                results.append(BulkRowResult(i, None, tuple(ex.errors)))
                continue

            if compensate is not None:
                for done in reversed(results):
                    if done.succeeded and done.result is not None:
                        compensate(done.result)
            raise

    return BulkResult(tuple(results))


def _ensure_size(count: int) -> None:
    ensure(
        0 < count <= BulkService.MAX_ROWS,
        ErrorCodes.LIMIT_EXCEEDED,
        "rows",
        f"A batch must contain between 1 and {BulkService.MAX_ROWS} rows.",
    )
