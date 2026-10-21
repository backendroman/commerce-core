from __future__ import annotations

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ..channels.model import AllocationStrategy, Channel
from ..common.address import Address
from ..common.errors import DomainError, DomainException, ErrorCodes
from .model import Allocation, ClickAndCollectOption, PreorderAllocation, Reservation, Stock, Warehouse

if TYPE_CHECKING:
    from ..catalog.model import ProductVariant
    from ..orders.model import Order
    from ..persistence.store import Store
    from ..shipping.model import ShippingZone


@dataclass(frozen=True)
class StockScope:
    warehouses: tuple[Warehouse, ...]
    country: str | None


@dataclass(frozen=True)
class StockShortage:
    variant_id: uuid.UUID
    requested: int
    available: int


@dataclass(frozen=True)
class LineDemand:
    line_id: uuid.UUID
    variant_id: uuid.UUID
    quantity: int


@dataclass(frozen=True)
class AllocationPlan:
    allocations: tuple[Allocation, ...]
    preorder_allocations: tuple[PreorderAllocation, ...]
    shortages: tuple[StockShortage, ...]


def zone_covers_country(store: Store, zone: ShippingZone, country: str) -> bool:
    if zone.is_default:
        return not any(not z.is_default and country in z.countries for z in store.shipping_zones.values())
    return country in zone.countries


def warehouse_serves_country(store: Store, channel: Channel, warehouse_id: uuid.UUID, country: str) -> bool:
    return any(warehouse_id in z.warehouse_ids for z in _zones_covering_country(store, channel, country))


def _zones_covering_country(store: Store, channel: Channel, country: str) -> list[ShippingZone]:
    explicitly_covered: bool | None = None
    result = []
    for zone in store.shipping_zones.values():
        if zone.id not in channel.shipping_zone_ids:
            continue
        if zone.is_default:
            if explicitly_covered is None:
                explicitly_covered = any(
                    not z.is_default and country in z.countries for z in store.shipping_zones.values()
                )
            covers = not explicitly_covered
        else:
            covers = country in zone.countries
        if covers:
            result.append(zone)
    return result


def channel_warehouses(store: Store, channel: Channel) -> list[Warehouse]:
    return [w for id_ in channel.warehouse_ids if (w := store.warehouses.get(id_)) is not None]


def channel_warehouses_serving(store: Store, channel: Channel, country: str) -> list[Warehouse]:
    zones = _zones_covering_country(store, channel, country)
    return [w for w in channel_warehouses(store, channel) if any(w.id in z.warehouse_ids for z in zones)]


def stock_country(channel: Channel, shipping: Address | None, billing: Address | None, requires_shipping: bool) -> str:
    if shipping is not None:
        return shipping.country
    if not requires_shipping and billing is not None:
        return billing.country
    return channel.default_country


def scope(
    store: Store,
    channel: Channel,
    shipping: Address | None,
    billing: Address | None,
    requires_shipping: bool,
    collection_point_id: uuid.UUID | None,
) -> StockScope:
    if collection_point_id is not None:
        point = store.warehouses.get(collection_point_id)
        if point is not None:
            if point.click_and_collect == ClickAndCollectOption.LOCAL:
                return StockScope((point,), point.address.country)
            return StockScope(tuple(channel_warehouses(store, channel)), point.address.country)

    country = stock_country(channel, shipping, billing, requires_shipping)
    return StockScope(tuple(channel_warehouses_serving(store, channel, country)), country)


def available(store: Store, stock: Stock, now: datetime, exclude_checkout_id: uuid.UUID | None = None) -> int:
    reserved = 0
    for r in store.reservations.for_stock(stock.warehouse_id, stock.variant_id):
        if r.is_active_at(now) and r.checkout_id != exclude_checkout_id:
            reserved += r.quantity
    pending_preorder = pending_preorder_quantity(store, stock.warehouse_id, stock.variant_id, now)
    return max(0, stock.quantity_in_stock - stock.quantity_allocated - pending_preorder - reserved)


def pending_preorder_quantity(store: Store, warehouse_id: uuid.UUID, variant_id: uuid.UUID, now: datetime) -> int:
    variant = store.variants.get(variant_id)
    if variant is None or variant.preorder is None or variant.is_in_preorder_at(now):
        return 0
    total = 0
    for a in store.preorder_allocations.for_variant(variant_id):
        target = preorder_target_warehouse(store, a)
        if target is not None and target.id == warehouse_id:
            total += a.quantity
    return total


def preorder_target_warehouse(store: Store, allocation: PreorderAllocation) -> Warehouse | None:
    order = store.orders.get(allocation.order_id)
    channel = store.channels.get(order.channel_id) if order is not None else None
    if order is None or channel is None:
        return None

    candidates = [w for w in channel_warehouses(store, channel) if (w.id, allocation.variant_id) in store.stocks]
    method = None
    if order.delivery is not None and order.delivery.kind == "shipping" and order.delivery.method_id is not None:
        try:
            method = store.find_shipping_method(uuid.UUID(order.delivery.method_id))
        except ValueError:
            method = None
    if method is not None:
        zone = store.shipping_zones[method.shipping_zone_id]
        candidates = [w for w in candidates if w.id in zone.warehouse_ids]
    else:
        country = order_country(order, channel)
        candidates = [w for w in candidates if warehouse_serves_country(store, channel, w.id, country)]

    candidates.sort(key=lambda w: (w.created_at, w.id))
    return candidates[0] if candidates else None


def order_country(order: Order, channel: Channel) -> str:
    if order.shipping_address is not None:
        return order.shipping_address.country
    if order.billing_address is not None:
        return order.billing_address.country
    return channel.default_country


def preorder_remaining(
    store: Store, variant: ProductVariant, channel_id: uuid.UUID, now: datetime, exclude_checkout_id: uuid.UUID | None
) -> tuple[int | None, int | None]:
    active_reservations = [
        r
        for r in store.reservations.for_preorder(variant.id)
        if r.preorder_channel_id is not None and r.is_active_at(now) and r.checkout_id != exclude_checkout_id
    ]
    allocations = store.preorder_allocations.for_variant(variant.id)

    global_left = None
    if variant.preorder is not None and variant.preorder.global_threshold is not None:
        global_left = (
            variant.preorder.global_threshold
            - sum(a.quantity for a in allocations)
            - sum(r.quantity for r in active_reservations)
        )
    channel_left = None
    listing = variant.channel_listings.get(channel_id)
    if listing is not None and listing.preorder_quantity_threshold is not None:
        channel_left = (
            listing.preorder_quantity_threshold
            - sum(a.quantity for a in allocations if a.channel_id == channel_id)
            - sum(r.quantity for r in active_reservations if r.preorder_channel_id == channel_id)
        )
    return global_left, channel_left


def find_shortages(
    store: Store,
    channel: Channel,
    scope: StockScope,
    demand: Iterable[tuple[uuid.UUID, int]],
    now: datetime,
    exclude_checkout_id: uuid.UUID | None,
) -> list[StockShortage]:
    grouped: dict[uuid.UUID, int] = {}
    for variant_id, quantity in demand:
        grouped[variant_id] = grouped.get(variant_id, 0) + quantity

    shortages: list[StockShortage] = []
    for variant_id, quantity in grouped.items():
        variant = store.variants.get(variant_id)
        if variant is None:
            continue

        if variant.is_in_preorder_at(now):
            global_left, channel_left = preorder_remaining(store, variant, channel.id, now, exclude_checkout_id)
            limits = [x for x in (global_left, channel_left) if x is not None]
            left = min(limits, default=None)
            if left is not None and quantity > left:
                shortages.append(StockShortage(variant.id, quantity, max(0, left)))
            continue

        stocks = _stocks_in(store, scope, variant.id)
        if not stocks:
            shortages.append(StockShortage(variant.id, quantity, 0))
            continue

        if not variant.track_inventory or variant.is_digital:
            continue
        total_available = sum(available(store, s, now, exclude_checkout_id) for s in stocks)
        if quantity > total_available:
            shortages.append(StockShortage(variant.id, quantity, total_available))

    return shortages


def is_in_stock(
    store: Store,
    channel: Channel,
    scope: StockScope,
    variant_id: uuid.UUID,
    now: datetime,
    exclude_checkout_id: uuid.UUID | None,
) -> bool:
    return not find_shortages(store, channel, scope, [(variant_id, 1)], now, exclude_checkout_id)


def _stocks_in(store: Store, scope: StockScope, variant_id: uuid.UUID) -> list[Stock]:
    return [s for w in scope.warehouses if (s := store.find_stock(w.id, variant_id)) is not None]


def order_by_strategy(
    store: Store,
    channel: Channel,
    scope: StockScope,
    variant_id: uuid.UUID,
    now: datetime,
    exclude_checkout_id: uuid.UUID | None,
) -> list[Stock]:
    stocks = _stocks_in(store, scope, variant_id)
    if channel.allocation_strategy == AllocationStrategy.PRIORITIZE_HIGH_STOCK:
        return sorted(stocks, key=lambda s: -available(store, s, now, exclude_checkout_id))
    return stocks  # already sorted by channel priority


def reserve(
    store: Store,
    channel: Channel,
    scope: StockScope,
    checkout_id: uuid.UUID,
    lines: Sequence[LineDemand],
    now: datetime,
    duration: timedelta,
) -> None:
    store.reservations.remove_for_checkout(checkout_id)
    if duration <= timedelta(0):
        return
    until = now + duration
    newly_reserved: dict[tuple[uuid.UUID, uuid.UUID], int] = {}

    for line in lines:
        variant = store.variants.get(line.variant_id)
        if variant is None:
            continue
        if variant.is_in_preorder_at(now):
            store.reservations.add(
                Reservation(
                    checkout_id=checkout_id,
                    checkout_line_id=line.line_id,
                    variant_id=variant.id,
                    preorder_channel_id=channel.id,
                    quantity=line.quantity,
                    reserved_until=until,
                )
            )
            continue

        if not variant.track_inventory or variant.is_digital:
            continue
        remaining = line.quantity
        for stock in order_by_strategy(store, channel, scope, variant.id, now, checkout_id):
            if remaining == 0:
                break
            key = (stock.warehouse_id, stock.variant_id)
            free = available(store, stock, now, checkout_id) - newly_reserved.get(key, 0)
            take = min(free, remaining)
            if take <= 0:
                continue
            store.reservations.add(
                Reservation(
                    checkout_id=checkout_id,
                    checkout_line_id=line.line_id,
                    variant_id=variant.id,
                    warehouse_id=stock.warehouse_id,
                    quantity=take,
                    reserved_until=until,
                )
            )
            newly_reserved[key] = newly_reserved.get(key, 0) + take
            remaining -= take


def plan_allocations(
    store: Store,
    channel: Channel,
    scope: StockScope,
    order_id: uuid.UUID,
    lines: Sequence[tuple[uuid.UUID, uuid.UUID, int]],
    now: datetime,
    source_checkout_id: uuid.UUID,
) -> AllocationPlan:
    allocations: list[Allocation] = []
    preorders: list[PreorderAllocation] = []
    used: dict[tuple[uuid.UUID, uuid.UUID], int] = {}
    shortages = find_shortages(store, channel, scope, [(v, q) for _, v, q in lines], now, source_checkout_id)
    if shortages:
        return AllocationPlan((), (), tuple(shortages))

    for order_line_id, variant_id, quantity in lines:
        variant = store.variants[variant_id]
        if variant.is_in_preorder_at(now):
            preorders.append(
                PreorderAllocation(
                    order_id=order_id,
                    order_line_id=order_line_id,
                    channel_id=channel.id,
                    variant_id=variant.id,
                    quantity=quantity,
                )
            )
            continue

        candidates = order_by_strategy(store, channel, scope, variant.id, now, source_checkout_id)
        if not variant.track_inventory or variant.is_digital:
            allocations.append(
                Allocation(
                    order_id=order_id,
                    order_line_id=order_line_id,
                    warehouse_id=candidates[0].warehouse_id,
                    variant_id=variant.id,
                    quantity=quantity,
                )
            )
            continue

        remaining = quantity
        for stock in candidates:
            if remaining == 0:
                break
            key = (stock.warehouse_id, stock.variant_id)
            take = min(available(store, stock, now, source_checkout_id) - used.get(key, 0), remaining)
            if take <= 0:
                continue
            allocations.append(
                Allocation(
                    order_id=order_id,
                    order_line_id=order_line_id,
                    warehouse_id=stock.warehouse_id,
                    variant_id=variant.id,
                    quantity=take,
                )
            )
            used[key] = used.get(key, 0) + take
            remaining -= take

        if remaining > 0:
            return AllocationPlan((), (), (StockShortage(variant.id, quantity, quantity - remaining),))

    return AllocationPlan(tuple(allocations), tuple(preorders), ())


def apply(store: Store, plan: AllocationPlan) -> None:
    for allocation in plan.allocations:
        store.stocks[(allocation.warehouse_id, allocation.variant_id)].quantity_allocated += allocation.quantity
        store.allocations.append(allocation)
    store.preorder_allocations.extend(plan.preorder_allocations)


def shortage_error(shortages: Sequence[StockShortage]) -> DomainException:
    return DomainException(
        [
            DomainError(
                ErrorCodes.INSUFFICIENT_STOCK,
                "lines",
                f"Insufficient stock for variant {s.variant_id}: requested {s.requested}, available {s.available}.",
                (s.variant_id,),
            )
            for s in shortages
        ]
    )
