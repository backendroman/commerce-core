from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from ..common.address import Address
from ..common.errors import ErrorCodes, ErrorCollector, must_exist
from ..events import payloads
from ..events.domain_event import EventTypes
from ..inventory import stock_logic
from ..permissions.model import Permission, Principal
from ..persistence.store import Store
from ..shop_context import ShopContext, require_permission
from .model import (
    PostalCodeRange,
    PostalCodeRuleMode,
    ShippingMethod,
    ShippingMethodChannelListing,
    ShippingMethodType,
    ShippingZone,
)

if TYPE_CHECKING:
    from ..channels.model import Channel


@dataclass(frozen=True, kw_only=True)
class ShippingZoneInput:
    name: str
    countries: tuple[str, ...] = ()
    is_default: bool = False
    warehouse_ids: tuple[uuid.UUID, ...] = ()
    channel_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ShippingMethodInput:
    zone_id: uuid.UUID
    name: str
    type: ShippingMethodType = ShippingMethodType.PRICE
    description: str = ""
    channel_listings: tuple[ShippingMethodChannelListing, ...] = ()
    minimum_weight_kg: Decimal | None = None
    maximum_weight_kg: Decimal | None = None
    minimum_delivery_days: int | None = None
    maximum_delivery_days: int | None = None
    tax_class_id: uuid.UUID | None = None
    postal_code_ranges: tuple[PostalCodeRange, ...] = ()
    postal_code_mode: PostalCodeRuleMode = PostalCodeRuleMode.EXCLUDE
    excluded_product_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True)
class ShippingContext:
    channel: Channel
    address: Address | None
    requires_shipping: bool
    subtotal_base: Decimal
    weight_kg: Decimal
    product_ids: frozenset[uuid.UUID]


def _gt(a, b) -> bool:
    return a is not None and b is not None and a > b


class ShippingService:
    def __init__(self, ctx: ShopContext):
        self._ctx = ctx

    def create_zone(self, principal: Principal, data: ShippingZoneInput) -> ShippingZone:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_SHIPPING)

        def create(store: Store) -> ShippingZone:
            errors = ErrorCollector()
            if data.is_default and any(z.is_default for z in store.shipping_zones.values()):
                errors.add(ErrorCodes.DUPLICATE, "default", "Only one zone can be the rest-of-world zone.")
            for id_ in data.warehouse_ids:
                if id_ not in store.warehouses:
                    errors.add(ErrorCodes.NOT_FOUND, "warehouses", f"Warehouse {id_} not found.")
            for id_ in data.channel_ids:
                if id_ not in store.channels:
                    errors.add(ErrorCodes.NOT_FOUND, "channels", f"Channel {id_} not found.")
            errors.raise_if_any()

            zone = ShippingZone(name=data.name, is_default=data.is_default)
            zone.countries.update(c.upper() for c in data.countries)
            zone.warehouse_ids.update(data.warehouse_ids)
            store.shipping_zones[zone.id] = zone
            for channel_id in data.channel_ids:
                store.channels[channel_id].shipping_zone_ids.add(zone.id)
            ctx.raise_event(EventTypes.SHIPPING_ZONE_CREATED, payloads.ref(zone.id, zone.name))
            return zone

        return ctx.db.write(create)

    def create_method(self, principal: Principal, data: ShippingMethodInput) -> ShippingMethod:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_SHIPPING)

        def create(store: Store) -> ShippingMethod:
            zone = must_exist(store.shipping_zones.get(data.zone_id), "shippingZone", data.zone_id)
            errors = ErrorCollector()
            for listing in data.channel_listings:
                if listing.channel_id not in store.channels:
                    errors.add(ErrorCodes.NOT_FOUND, "channelListings", f"Channel {listing.channel_id} not found.")
                if listing.price < 0:
                    errors.add(ErrorCodes.INVALID, "price", "Price cannot be negative.")
                if _gt(listing.minimum_order_price, listing.maximum_order_price):
                    errors.add(ErrorCodes.INVALID, "maximumOrderPrice", "Maximum must be >= minimum.")

            if _gt(data.minimum_weight_kg, data.maximum_weight_kg):
                errors.add(ErrorCodes.INVALID, "maximumOrderWeight", "Maximum must be >= minimum.")
            if _gt(data.minimum_delivery_days, data.maximum_delivery_days):
                errors.add(ErrorCodes.INVALID, "maximumDeliveryDays", "Maximum must be >= minimum.")
            errors.raise_if_any()

            method = ShippingMethod(
                shipping_zone_id=zone.id,
                name=data.name,
                description=data.description,
                type=data.type,
                minimum_weight_kg=data.minimum_weight_kg,
                maximum_weight_kg=data.maximum_weight_kg,
                minimum_delivery_days=data.minimum_delivery_days,
                maximum_delivery_days=data.maximum_delivery_days,
                tax_class_id=data.tax_class_id,
                postal_code_mode=data.postal_code_mode,
            )
            for listing in data.channel_listings:
                method.channel_listings[listing.channel_id] = listing
            method.postal_code_ranges.extend(data.postal_code_ranges)
            method.excluded_product_ids.update(data.excluded_product_ids)
            zone.methods.append(method)
            ctx.raise_event(EventTypes.SHIPPING_PRICE_CREATED, payloads.ref(method.id, method.name))
            return method

        return ctx.db.write(create)

    @staticmethod
    def available_methods(store: Store, c: ShippingContext) -> list[ShippingMethod]:
        if not c.requires_shipping or c.address is None:
            return []
        return [
            m
            for z in store.shipping_zones.values()
            if z.id in c.channel.shipping_zone_ids and stock_logic.zone_covers_country(store, z, c.address.country)
            for m in z.methods
            if ShippingService.is_applicable(m, c)
        ]

    @staticmethod
    def is_applicable(method: ShippingMethod, c: ShippingContext) -> bool:
        listing = method.channel_listings.get(c.channel.id)
        if listing is None:
            return False
        if method.type == ShippingMethodType.PRICE:
            # free shipping threshold: subtotal after promos but before vouchers
            if listing.minimum_order_price is not None and c.subtotal_base < listing.minimum_order_price:
                return False
            if listing.maximum_order_price is not None and c.subtotal_base > listing.maximum_order_price:
                return False
        else:
            if method.minimum_weight_kg is not None and c.weight_kg < method.minimum_weight_kg:
                return False
            if method.maximum_weight_kg is not None and c.weight_kg > method.maximum_weight_kg:
                return False

        if c.address is not None and not method.postal_code_allowed(c.address.postal_code):
            return False
        return method.excluded_product_ids.isdisjoint(c.product_ids)

    @staticmethod
    def is_method_available(store: Store, method_id: uuid.UUID, c: ShippingContext) -> bool:
        return any(m.id == method_id for m in ShippingService.available_methods(store, c))
