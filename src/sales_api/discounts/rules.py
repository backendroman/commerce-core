from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from ..common.errors import DomainError, ErrorCodes
from ..common.money import ZERO, dsum, fmt, round_to_currency
from .model import DiscountValueType, Promotion, PromotionRule, PromotionType, Voucher, VoucherCode, VoucherType

if TYPE_CHECKING:
    from ..channels.model import Channel
    from ..persistence.store import Store


@dataclass(frozen=True)
class VoucherContext:
    channel: Channel
    email: str | None
    user_id: uuid.UUID | None
    is_active_staff_user: bool
    subtotal_base: Decimal
    items_quantity: int
    shipping_country: str | None


def is_valid_code_format(code: str) -> bool:
    return 4 <= len(code) <= 32 and all((c.isascii() and c.isalnum()) or c == "-" for c in code)


def normalize_code(code: str) -> str:
    return code.strip().upper()


def validate_voucher(voucher: Voucher, code: VoucherCode, c: VoucherContext, now: datetime) -> DomainError | None:
    def fail(message: str) -> DomainError:
        return DomainError(ErrorCodes.CODE_REJECTED, "promoCode", message)

    if not voucher.is_active_at(now) or not code.is_active:
        return fail("Promo code is not active.")
    listing = voucher.channel_listings.get(c.channel.id)
    if listing is None:
        return fail("Promo code is not available in this channel.")
    if voucher.single_use and code.used > 0:
        return fail("Promo code has already been used.")
    if voucher.usage_limit is not None and voucher.total_used >= voucher.usage_limit:
        return fail("Promo code usage limit reached.")
    if voucher.apply_once_per_customer:
        # Counted by email so that guest orders are included.
        if c.email is None or not c.email.strip():
            return fail("An email is required to use this promo code.")
        if c.email in voucher.customer_emails_used:
            return fail("Promo code has already been used by this customer.")

    # active staff only
    if voucher.only_for_staff and not (c.user_id is not None and c.is_active_staff_user):
        return fail("Promo code is for staff only.")
    if listing.min_spent is not None and c.subtotal_base < listing.min_spent:
        return fail(f"Minimum order value is {fmt(listing.min_spent)} {c.channel.currency}.")
    if voucher.min_checkout_items_quantity is not None and c.items_quantity < voucher.min_checkout_items_quantity:
        return fail(f"Minimum quantity is {voucher.min_checkout_items_quantity}.")
    if (
        voucher.type == VoucherType.SHIPPING
        and voucher.countries
        and c.shipping_country is not None
        and c.shipping_country not in voucher.countries
    ):
        return fail("Promo code is not valid for the shipping country.")
    return None


def amount_off(kind: DiscountValueType, value: Decimal, base_amount: Decimal, currency: str) -> Decimal:
    off = round_to_currency(base_amount * value / 100, currency) if kind == DiscountValueType.PERCENTAGE else value
    return min(base_amount, off)


def distribute_proportionally(total: Decimal, weights: Sequence[Decimal], currency: str) -> list[Decimal]:
    shares = [ZERO] * len(weights)
    weight_sum = dsum(weights)
    if weight_sum <= 0 or total <= 0:
        return shares

    last = -1
    for i, w in enumerate(weights):
        if w > 0:
            last = i

    allocated = ZERO
    for i, w in enumerate(weights):
        if w <= 0 or i == last:
            continue
        shares[i] = min(w, round_to_currency(total * w / weight_sum, currency))
        allocated += shares[i]

    shares[last] = min(weights[last], total - allocated)
    return shares


@dataclass(frozen=True)
class OrderRewardCandidate:
    promotion: Promotion
    rule: PromotionRule
    benefit: Decimal
    gift_variant_id: uuid.UUID | None


def order_rule_satisfied(
    store: Store, channel: Channel, subtotal_after_catalogue: Decimal, now: datetime
) -> list[tuple[Promotion, PromotionRule]]:
    promotions = sorted(
        (p for p in store.promotions.values() if p.type == PromotionType.ORDER and p.is_active_at(now)),
        key=lambda p: (p.starts_at, p.id),
    )
    return [
        (p, r)
        for p in promotions
        for r in p.rules
        if channel.id in r.channel_ids
        and r.condition is not None
        and r.condition.is_satisfied(subtotal_after_catalogue)
    ]


def best_order_reward(
    satisfied: Iterable[tuple[Promotion, PromotionRule]],
    discount_base: Decimal,
    currency: str,
    gift_value: Callable[[uuid.UUID], Decimal | None],
    preferred_gift_variant_id: uuid.UUID | None,
) -> OrderRewardCandidate | None:
    best: OrderRewardCandidate | None = None
    for promotion, rule in satisfied:
        if rule.is_gift_reward:
            options = [
                (id_, index, value)
                for index, id_ in enumerate(rule.gift_variant_ids)
                if (value := gift_value(id_)) is not None
            ]
            if not options:
                continue
            chosen = next((o for o in options if o[0] == preferred_gift_variant_id), None)
            if chosen is None:
                chosen = sorted(options, key=lambda o: (-o[2], o[1]))[0]
            candidate = OrderRewardCandidate(promotion, rule, chosen[2], chosen[0])
        elif rule.value_type is not None and rule.value is not None:
            candidate = OrderRewardCandidate(
                promotion, rule, amount_off(rule.value_type, rule.value, discount_base, currency), None
            )
        else:
            continue

        if best is None or candidate.benefit > best.benefit:
            best = candidate

    return best
