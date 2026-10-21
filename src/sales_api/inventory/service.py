from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from ..catalog.model import ProductVariant
from ..common.address import Address
from ..common.errors import DomainException, ErrorCodes, ErrorCollector, ensure, must_exist
from ..common.slug import unique_slug
from ..events import payloads
from ..events.domain_event import EventTypes
from ..permissions.model import Permission, Principal
from ..persistence.store import Store
from ..shop_context import ShopContext, require_permission
from . import stock_logic
from .model import Allocation, ClickAndCollectOption, Stock, Warehouse


@dataclass(frozen=True, kw_only=True)
class WarehouseCreateInput:
    name: str
    address: Address
    slug: str | None = None
    email: str = ""
    click_and_collect: ClickAndCollectOption = ClickAndCollectOption.DISABLED


@dataclass(frozen=True)
class StockInput:
    variant_id: uuid.UUID
    warehouse_id: uuid.UUID
    quantity: int
    low_stock_threshold: int | None = None


class InventoryService:
    PREORDER_FAILURE_EVENT_INTERVAL = timedelta(days=1)

    def __init__(self, ctx: ShopContext):
        self._ctx = ctx

    def create_warehouse(self, principal: Principal, data: WarehouseCreateInput) -> Warehouse:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_PRODUCTS)
        address = ctx.addresses.normalize(data.address)

        def create(store: Store) -> Warehouse:
            warehouse = Warehouse(
                name=data.name.strip(),
                slug=unique_slug(data.slug, data.name, lambda s: any(w.slug == s for w in store.warehouses.values())),
                address=address,
                email=data.email,
                click_and_collect=data.click_and_collect,
                created_at=ctx.now,
            )
            store.warehouses[warehouse.id] = warehouse
            ctx.raise_event(EventTypes.WAREHOUSE_CREATED, payloads.ref(warehouse.id, warehouse.name))
            return warehouse

        return ctx.db.write(create)

    def set_click_and_collect(
        self, principal: Principal, warehouse_id: uuid.UUID, option: ClickAndCollectOption
    ) -> None:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_PRODUCTS)

        def update(store: Store) -> None:
            must_exist(store.warehouses.get(warehouse_id), "warehouse", warehouse_id).click_and_collect = option
            ctx.raise_event(EventTypes.WAREHOUSE_UPDATED, payloads.ref(warehouse_id, ""))

        ctx.db.write(update)

    def update_stocks(self, principal: Principal, stocks: Sequence[StockInput]) -> None:
        require_permission(principal, Permission.MANAGE_PRODUCTS)

        def update(store: Store) -> None:
            errors = ErrorCollector()
            for s in stocks:
                if s.variant_id not in store.variants:
                    errors.add(ErrorCodes.NOT_FOUND, "variantId", f"Variant {s.variant_id} not found.")
                if s.warehouse_id not in store.warehouses:
                    errors.add(ErrorCodes.NOT_FOUND, "warehouseId", f"Warehouse {s.warehouse_id} not found.")
                if s.quantity < 0:
                    errors.add(ErrorCodes.INVALID, "quantity", "Quantity cannot be negative.")

            errors.raise_if_any()
            for s in stocks:
                self.set_stock(store, s)

        self._ctx.db.write(update)

    def set_stock(self, store: Store, data: StockInput) -> None:
        key = (data.warehouse_id, data.variant_id)
        before = self.total_available(store, data.variant_id)
        stock = store.stocks.get(key)
        if stock is None:
            stock = Stock(warehouse_id=data.warehouse_id, variant_id=data.variant_id)
            store.stocks[key] = stock

        # quantity < allocated is allowed, available() clamps to 0
        stock.quantity_in_stock = data.quantity
        if data.low_stock_threshold is not None:
            stock.low_stock_threshold = data.low_stock_threshold
        self.emit_stock_events(store, data.variant_id, before, stock)

    def emit_stock_events(
        self, store: Store, variant_id: uuid.UUID, available_before: int, changed_stock: Stock | None = None
    ) -> None:
        ctx = self._ctx
        variant = store.variants[variant_id]
        after = self.total_available(store, variant_id)
        payload = payloads.variant(store, variant)
        ctx.raise_event(EventTypes.PRODUCT_VARIANT_STOCK_UPDATED, payload)
        if available_before > 0 and after == 0:
            ctx.raise_event(EventTypes.PRODUCT_VARIANT_OUT_OF_STOCK, payload)
        if available_before == 0 and after > 0:
            ctx.raise_event(EventTypes.PRODUCT_VARIANT_BACK_IN_STOCK, payload)
        if (
            changed_stock is not None
            and changed_stock.low_stock_threshold is not None
            and stock_logic.available(store, changed_stock, ctx.now) <= changed_stock.low_stock_threshold
        ):
            ctx.raise_event(EventTypes.PRODUCT_VARIANT_LOW_STOCK, payload)

    def total_available(self, store: Store, variant_id: uuid.UUID) -> int:
        return sum(stock_logic.available(store, s, self._ctx.now) for s in store.stocks_of_variant(variant_id))

    def complete_preorder(self, principal: Principal, variant_id: uuid.UUID) -> None:
        require_permission(principal, Permission.MANAGE_PRODUCTS)

        def complete(store: Store) -> None:
            variant = must_exist(store.variants.get(variant_id), "variant", variant_id)
            ensure(variant.preorder is not None, ErrorCodes.INVALID, "variant", "Variant is not in preorder.")
            if not self._try_migrate_preorder(store, variant):
                raise DomainException(
                    ErrorCodes.PREORDER_WAREHOUSE_MISSING,
                    "variant",
                    "No eligible warehouse for preorder allocations; variant stays in preorder.",
                )

        self._ctx.db.write(complete)

    def migrate_ended_preorders(self) -> int:
        ctx = self._ctx

        def migrate(store: Store) -> int:
            migrated = 0
            ended = [
                v
                for v in store.variants.values()
                if v.preorder is not None and v.preorder.end_date is not None and v.preorder.end_date <= ctx.now
            ]
            for variant in ended:
                if self._try_migrate_preorder(store, variant):
                    migrated += 1
                    continue

                # notify on the first failure, then at most daily
                last = variant.preorder_migration_failure_notified_at
                if last is None or ctx.now - last >= self.PREORDER_FAILURE_EVENT_INTERVAL:
                    variant.preorder_migration_failure_notified_at = ctx.now
                    ctx.raise_event(EventTypes.PREORDER_MIGRATION_FAILED, payloads.variant(store, variant))

            return migrated

        return ctx.db.write(migrate)

    def _try_migrate_preorder(self, store: Store, variant: ProductVariant) -> bool:
        ctx = self._ctx
        preorder_allocations = list(store.preorder_allocations.for_variant(variant.id))
        targets = []
        for allocation in preorder_allocations:
            warehouse = stock_logic.preorder_target_warehouse(store, allocation)
            if warehouse is None:
                return False
            targets.append((allocation, warehouse))

        for preorder, warehouse in targets:
            # intentionally no availability check
            store.stocks[(warehouse.id, variant.id)].quantity_allocated += preorder.quantity
            store.allocations.append(
                Allocation(
                    order_id=preorder.order_id,
                    order_line_id=preorder.order_line_id,
                    warehouse_id=warehouse.id,
                    variant_id=variant.id,
                    quantity=preorder.quantity,
                )
            )
            store.preorder_allocations.remove(preorder)

        variant.preorder = None
        variant.preorder_migration_failure_notified_at = None
        variant.updated_at = ctx.now
        ctx.raise_event(EventTypes.PRODUCT_VARIANT_UPDATED, payloads.variant(store, variant))
        return True
