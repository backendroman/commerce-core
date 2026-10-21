from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from ..common.money import ZERO, round_to_currency
from .model import CatalogueSelection, DiscountValueType, Promotion, PromotionRule, PromotionType

if TYPE_CHECKING:
    from ..catalog.model import Product, ProductVariant
    from ..channels.model import Channel
    from ..persistence.store import Store


@dataclass(frozen=True)
class CatalogueDiscountResult:
    price: Decimal
    discount_per_unit: Decimal
    rule: PromotionRule | None
    promotion: Promotion | None


def active_promotions(store: Store, now: datetime) -> list[Promotion]:
    return [p for p in store.promotions.values() if p.type == PromotionType.CATALOGUE and p.is_active_at(now)]


def discounted_unit_price(
    store: Store,
    variant: ProductVariant,
    channel: Channel,
    unit_price: Decimal,
    promotions: Sequence[Promotion] | datetime,
) -> CatalogueDiscountResult:
    """Best discount for one unit. `promotions` can also be a datetime to look them up at."""
    if isinstance(promotions, datetime):
        promotions = active_promotions(store, promotions)

    best = CatalogueDiscountResult(unit_price, ZERO, None, None)
    product = store.products.get(variant.product_id)
    if product is None:
        return best

    for promotion in promotions:
        for rule in promotion.rules:
            if channel.id not in rule.channel_ids or not matches(store, rule.selection, product, variant):
                continue
            discount = unit_discount(rule, unit_price, channel.currency)
            if discount > best.discount_per_unit:
                best = CatalogueDiscountResult(
                    max(ZERO, round_to_currency(unit_price - discount, channel.currency)),
                    min(discount, unit_price),
                    rule,
                    promotion,
                )

    return best


def unit_discount(rule: PromotionRule, unit_price: Decimal, currency: str) -> Decimal:
    if rule.value is None or rule.value_type is None:
        return ZERO
    if rule.value_type == DiscountValueType.PERCENTAGE:
        discount = round_to_currency(unit_price * rule.value / 100, currency)
    else:
        discount = rule.value
    return min(discount, unit_price)


def matches(store: Store, selection: CatalogueSelection, product: Product, variant: ProductVariant) -> bool:
    if variant.id in selection.variant_ids or product.id in selection.product_ids:
        return True
    if product.category_id is not None and selection.category_ids:
        current: uuid.UUID | None = product.category_id
        while current is not None:
            if current in selection.category_ids:
                return True
            category = store.categories.get(current)
            current = category.parent_id if category is not None else None

    return any(
        (c := store.collections.get(id_)) is not None and product.id in c.product_ids
        for id_ in selection.collection_ids
    )
