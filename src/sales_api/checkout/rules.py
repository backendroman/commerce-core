from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum, auto
from typing import TYPE_CHECKING

from ..common.errors import DomainError, ErrorCodes
from ..inventory import stock_logic
from ..inventory.stock_logic import StockScope
from .model import Checkout, DeliveryKind

if TYPE_CHECKING:
    from ..catalog.model import ProductVariant
    from ..channels.model import Channel
    from ..persistence.store import Store


class CheckoutProblemType(StrEnum):
    INSUFFICIENT_STOCK = auto()
    VARIANT_NOT_AVAILABLE = auto()


@dataclass(frozen=True)
class CheckoutProblem:
    type: CheckoutProblemType
    line_id: uuid.UUID
    variant_id: uuid.UUID
    message: str
    available_quantity: int | None = None


def unavailability_reason(store: Store, channel: Channel, variant: ProductVariant, now: datetime) -> str | None:
    if not channel.is_active:
        return "Channel is inactive."
    product = store.products.get(variant.product_id)
    listing = product.channel_listings.get(channel.id) if product is not None else None
    if listing is None:
        return "Product is not available in this channel."
    if not listing.is_visible_at(now):
        return "Product is not published."
    if not listing.is_purchasable_at(now):
        return "Product is not available for purchase."
    variant_listing = variant.channel_listings.get(channel.id)
    if variant_listing is None or variant_listing.price is None:
        return "Variant has no price in this channel."
    return None


def problems(store: Store, checkout: Checkout, channel: Channel, now: datetime) -> list[CheckoutProblem]:
    result: list[CheckoutProblem] = []
    for line in checkout.lines:
        variant = store.variants.get(line.variant_id)
        reason = "Variant no longer exists." if variant is None else unavailability_reason(store, channel, variant, now)
        if reason is not None:
            result.append(CheckoutProblem(CheckoutProblemType.VARIANT_NOT_AVAILABLE, line.id, line.variant_id, reason))

    scope = stock_scope_of(store, checkout, channel)
    demand = [(line.variant_id, line.quantity) for line in checkout.lines if line.variant_id in store.variants]
    for shortage in stock_logic.find_shortages(store, channel, scope, demand, now, checkout.id):
        result.extend(
            CheckoutProblem(
                CheckoutProblemType.INSUFFICIENT_STOCK,
                line.id,
                line.variant_id,
                f"Only {shortage.available} available.",
                shortage.available,
            )
            for line in checkout.lines
            if line.variant_id == shortage.variant_id
        )

    return result


def requires_shipping(store: Store, checkout: Checkout) -> bool:
    return any(
        (v := store.variants.get(line.variant_id)) is not None
        and store.product_types[store.products[v.product_id].product_type_id].is_shipping_required
        for line in checkout.lines
    )


def stock_scope_of(store: Store, checkout: Checkout, channel: Channel) -> StockScope:
    return stock_logic.scope(
        store,
        channel,
        checkout.shipping_address,
        checkout.billing_address,
        requires_shipping(store, checkout),
        checkout.delivery.collection_point_warehouse_id
        if checkout.delivery.kind == DeliveryKind.COLLECTION_POINT
        else None,
    )


def problem_to_error(p: CheckoutProblem) -> DomainError:
    return DomainError(
        ErrorCodes.INSUFFICIENT_STOCK
        if p.type == CheckoutProblemType.INSUFFICIENT_STOCK
        else ErrorCodes.VARIANT_UNAVAILABLE,
        "lines",
        p.message,
        (p.variant_id,),
    )
