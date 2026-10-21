from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from ..catalog.model import Product, ProductType, ProductVariant
from ..common.errors import DomainError, ErrorCodes
from ..common.money import ZERO, Money, TaxedMoney, dsum, round_to_currency, taxed_sum
from ..discounts import catalogue as catalogue_discounts
from ..discounts.model import DiscountValueType, Voucher, VoucherType
from ..discounts.rules import (
    OrderRewardCandidate,
    VoucherContext,
    amount_off,
    best_order_reward,
    distribute_proportionally,
    order_rule_satisfied,
    validate_voucher,
)
from ..inventory import stock_logic
from ..inventory.stock_logic import StockScope
from ..orders.model import AppliedDiscount, AppliedGiftCard
from ..shipping.service import ShippingContext, ShippingService
from ..taxes.calculator import TaxableLine, TaxCalculator, TaxedLine, TaxRequest
from . import rules as checkout_rules
from .model import Checkout, CheckoutLine, DeliveryKind

if TYPE_CHECKING:
    from ..channels.model import Channel
    from ..persistence.store import Store
    from ..shop_context import ShopContext


@dataclass(frozen=True, kw_only=True)
class LinePricing:
    line_id: uuid.UUID
    variant_id: uuid.UUID
    total: TaxedMoney
    undiscounted_total: TaxedMoney
    unit_price: TaxedMoney
    quantity: int = 0
    is_gift: bool = False
    is_gift_card: bool = False
    requires_shipping: bool = False
    tax_class_id: uuid.UUID | None = None
    base_unit_price: Decimal = ZERO
    catalogue_unit_price: Decimal = ZERO
    undiscounted_total_base: Decimal = ZERO
    catalogue_total_base: Decimal = ZERO
    total_base: Decimal = ZERO
    tax_rate: Decimal = ZERO
    discounts: tuple[AppliedDiscount, ...] = ()


@dataclass(frozen=True, kw_only=True)
class CheckoutPricing:
    currency: str
    lines: tuple[LinePricing, ...]
    subtotal: TaxedMoney
    shipping: TaxedMoney
    total: TaxedMoney
    undiscounted_total: TaxedMoney
    subtotal_base: Decimal = ZERO
    shipping_tax_rate: Decimal = ZERO
    discounts: tuple[AppliedDiscount, ...] = ()
    voucher: Voucher | None = None
    voucher_error: DomainError | None = None
    order_reward: OrderRewardCandidate | None = None
    gift_cards: tuple[AppliedGiftCard, ...] = ()
    gift_card_errors: tuple[DomainError, ...] = ()
    amount_to_pay: Decimal = ZERO
    requires_shipping: bool = False
    delivery_valid: bool = False
    tax_country: str = ""
    weight_kg: Decimal = ZERO
    calculated_at: datetime | None = None


@dataclass(eq=False, kw_only=True)
class _Draft:
    line: CheckoutLine
    variant: ProductVariant
    product: Product
    type: ProductType
    base_unit: Decimal = ZERO
    catalogue_unit: Decimal = ZERO
    total: Decimal = ZERO
    discounts: list[AppliedDiscount] = field(default_factory=list)

    @property
    def undiscounted_total(self) -> Decimal:
        return self.base_unit * self.line.quantity

    @property
    def catalogue_total(self) -> Decimal:
        return self.catalogue_unit * self.line.quantity


class CheckoutCalculator:
    def __init__(self, ctx: ShopContext, taxes: TaxCalculator):
        self._ctx = ctx
        self._taxes = taxes

    def calculate(self, store: Store, checkout: Checkout) -> CheckoutPricing:
        channel = store.channels[checkout.channel_id]
        currency = channel.currency
        now = self._ctx.now

        drafts = [_build_draft(store, channel, line, now) for line in checkout.lines]
        regular = [d for d in drafts if not d.line.is_gift]
        # Thresholds (voucher min spend, order promos, free shipping) use the pre-voucher subtotal.
        subtotal_base = dsum(d.catalogue_total for d in regular)
        requires_shipping = any(d.type.is_shipping_required for d in drafts)
        weight = dsum(
            weight_of(d.variant, d.product, d.type) * d.line.quantity for d in drafts if d.type.is_shipping_required
        )

        voucher, voucher_error = _resolve_voucher(
            store, checkout, channel, subtotal_base, sum(d.line.quantity for d in regular), now
        )
        if voucher is not None and voucher.type == VoucherType.SPECIFIC_PRODUCT:
            _apply_specific_product_voucher(store, voucher, channel, regular)
        if voucher is not None and voucher.type == VoucherType.ENTIRE_ORDER:
            listing = voucher.channel_listings[channel.id]
            off = amount_off(voucher.value_type, listing.discount_value, dsum(d.total for d in regular), currency)
            _distribute_discount(
                regular, off, currency, lambda d: AppliedDiscount("VOUCHER", voucher.name, d, "Promo code", voucher.id)
            )

        # Order promotions don't stack with ENTIRE_ORDER vouchers.
        reward: OrderRewardCandidate | None = None
        if voucher is None or voucher.type != VoucherType.ENTIRE_ORDER:
            reward = self.resolve_order_reward(
                store, checkout, channel, subtotal_base, dsum(d.total for d in regular), now
            )
            if reward is not None and not reward.rule.is_gift_reward:
                _distribute_discount(
                    regular,
                    reward.benefit,
                    currency,
                    lambda d: AppliedDiscount(
                        "ORDER_PROMOTION", reward.promotion.name, d, "Order promotion", reward.rule.id
                    ),
                )

        shipping_base, shipping_tax_class, delivery_valid = _resolve_shipping(
            store, checkout, channel, subtotal_base, weight, requires_shipping, drafts
        )
        order_discounts: list[AppliedDiscount] = []
        if voucher is not None and voucher.type == VoucherType.SHIPPING and shipping_base > 0:
            off = amount_off(
                voucher.value_type, voucher.channel_listings[channel.id].discount_value, shipping_base, currency
            )
            shipping_base -= off
            order_discounts.append(AppliedDiscount("VOUCHER", voucher.name, off, "Shipping promo code", voucher.id))

        tax_country = tax_country_of(store, checkout, channel)
        tax_request = TaxRequest(
            channel,
            tax_country,
            tuple(
                TaxableLine(
                    d.line.id,
                    d.variant.id,
                    d.line.quantity,
                    max(ZERO, d.total),
                    d.product.tax_class_id or d.type.tax_class_id,
                    d.type.is_gift_card,
                )
                for d in drafts
            ),
            max(ZERO, shipping_base),
            shipping_tax_class,
            checkout.tax_exempt,
            {"shipping": checkout.shipping_address, "billing": checkout.billing_address},
        )
        taxed = self._taxes.calculate(store, tax_request)
        taxed_by_id: dict[uuid.UUID, TaxedLine] = {}
        for taxed_line in taxed.lines:
            taxed_by_id.setdefault(taxed_line.line_id, taxed_line)

        lines = []
        for d in drafts:
            t = taxed_by_id[d.line.id]
            lines.append(
                LinePricing(
                    line_id=d.line.id,
                    variant_id=d.variant.id,
                    quantity=d.line.quantity,
                    is_gift=d.line.is_gift,
                    is_gift_card=d.type.is_gift_card,
                    requires_shipping=d.type.is_shipping_required,
                    tax_class_id=d.product.tax_class_id or d.type.tax_class_id,
                    base_unit_price=d.base_unit,
                    catalogue_unit_price=d.catalogue_unit,
                    undiscounted_total_base=d.undiscounted_total,
                    catalogue_total_base=d.catalogue_total,
                    total_base=d.total,
                    total=t.total,
                    undiscounted_total=TaxCalculator.apply(
                        d.undiscounted_total, t.rate, channel.taxes.prices_entered_with_tax, currency
                    ),
                    unit_price=TaxedMoney(
                        Money(round_to_currency(t.total.net.amount / d.line.quantity, currency), currency),
                        Money(round_to_currency(t.total.gross.amount / d.line.quantity, currency), currency),
                    ),
                    tax_rate=t.rate,
                    discounts=tuple(d.discounts),
                )
            )

        subtotal = taxed_sum((line.total for line in lines), currency)
        total = subtotal + taxed.shipping
        undiscounted = taxed_sum((line.undiscounted_total for line in lines), currency) + taxed.shipping

        # Gift cards only reduce amount_to_pay (and can't pay for other gift cards).
        gift_card_eligible = total.gross.amount - dsum(line.total.gross.amount for line in lines if line.is_gift_card)
        gift_cards, gift_card_errors = _apply_gift_cards(store, checkout, currency, gift_card_eligible, now)

        return CheckoutPricing(
            currency=currency,
            lines=tuple(lines),
            subtotal_base=subtotal_base,
            subtotal=subtotal,
            shipping=taxed.shipping,
            shipping_tax_rate=taxed.shipping_rate,
            total=total,
            undiscounted_total=undiscounted,
            discounts=tuple(order_discounts),
            voucher=voucher,
            voucher_error=voucher_error,
            order_reward=reward,
            gift_cards=tuple(gift_cards),
            gift_card_errors=tuple(gift_card_errors),
            amount_to_pay=max(ZERO, total.gross.amount - dsum(g.amount for g in gift_cards)),
            requires_shipping=requires_shipping,
            delivery_valid=delivery_valid,
            tax_country=tax_country,
            weight_kg=weight,
            calculated_at=now,
        )

    def preview_order_reward(self, store: Store, checkout: Checkout) -> OrderRewardCandidate | None:
        channel = store.channels[checkout.channel_id]
        now = self._ctx.now
        regular = [_build_draft(store, channel, line, now) for line in checkout.lines if not line.is_gift]
        if not regular:
            return None
        subtotal_base = dsum(d.catalogue_total for d in regular)
        voucher, _ = _resolve_voucher(
            store, checkout, channel, subtotal_base, sum(d.line.quantity for d in regular), now
        )
        if voucher is not None and voucher.type == VoucherType.ENTIRE_ORDER:
            return None
        if voucher is not None and voucher.type == VoucherType.SPECIFIC_PRODUCT:
            _apply_specific_product_voucher(store, voucher, channel, regular)
        return self.resolve_order_reward(store, checkout, channel, subtotal_base, dsum(d.total for d in regular), now)

    def resolve_order_reward(
        self,
        store: Store,
        checkout: Checkout,
        channel: Channel,
        subtotal_base: Decimal,
        discount_base: Decimal,
        now: datetime,
    ) -> OrderRewardCandidate | None:
        satisfied = order_rule_satisfied(store, channel, subtotal_base, now)
        if not satisfied:
            return None
        scope = checkout_rules.stock_scope_of(store, checkout, channel)
        return best_order_reward(
            satisfied,
            discount_base,
            channel.currency,
            lambda variant_id: gift_value(store, channel, scope, checkout, variant_id, now),
            checkout.preferred_gift_variant_id,
        )


def gift_value(
    store: Store, channel: Channel, scope: StockScope, checkout: Checkout, variant_id: uuid.UUID, now: datetime
) -> Decimal | None:
    variant = store.variants.get(variant_id)
    if variant is None:
        return None
    if checkout_rules.unavailability_reason(store, channel, variant, now) is not None:
        return None
    if not stock_logic.is_in_stock(store, channel, scope, variant_id, now, checkout.id):
        return None
    price = variant.channel_listings[channel.id].price
    if price is None:
        return None
    return catalogue_discounts.discounted_unit_price(store, variant, channel, price, now).price


def weight_of(variant: ProductVariant, product: Product, kind: ProductType) -> Decimal:
    for w in (variant.weight_kg, product.weight_kg, kind.default_weight_kg):
        if w is not None:
            return w
    return ZERO


def tax_country_of(store: Store, checkout: Checkout, channel: Channel) -> str:
    if (
        checkout.delivery.kind == DeliveryKind.COLLECTION_POINT
        and checkout.delivery.collection_point_warehouse_id is not None
    ):
        w = store.warehouses.get(checkout.delivery.collection_point_warehouse_id)
        if w is not None:
            return w.address.country
    if checkout.shipping_address is not None:
        return checkout.shipping_address.country
    if checkout.billing_address is not None:
        return checkout.billing_address.country
    return channel.default_country


def _build_draft(store: Store, channel: Channel, line: CheckoutLine, now: datetime) -> _Draft:
    variant = store.variants[line.variant_id]
    product = store.products[variant.product_id]
    product_type = store.product_types[product.product_type_id]
    listing = variant.channel_listings.get(channel.id)
    listing_price = listing.price if listing is not None and listing.price is not None else ZERO

    if line.is_gift:
        return _Draft(line=line, variant=variant, product=product, type=product_type)

    if line.price_override is not None:
        # custom price from an app -> skip catalogue discounts
        overridden = line.price_override
        return _Draft(
            line=line,
            variant=variant,
            product=product,
            type=product_type,
            base_unit=overridden,
            catalogue_unit=overridden,
            total=overridden * line.quantity,
        )

    catalogue = catalogue_discounts.discounted_unit_price(store, variant, channel, listing_price, now)
    draft = _Draft(
        line=line,
        variant=variant,
        product=product,
        type=product_type,
        base_unit=listing_price,
        catalogue_unit=catalogue.price,
        total=catalogue.price * line.quantity,
    )
    if catalogue.promotion is not None and catalogue.rule is not None and catalogue.discount_per_unit > 0:
        draft.discounts.append(
            AppliedDiscount(
                "PROMOTION",
                catalogue.promotion.name,
                (listing_price - catalogue.price) * line.quantity,
                "Catalogue promotion",
                catalogue.rule.id,
            )
        )
    return draft


def _resolve_voucher(
    store: Store, checkout: Checkout, channel: Channel, subtotal_base: Decimal, quantity: int, now: datetime
) -> tuple[Voucher | None, DomainError | None]:
    if checkout.voucher_code is None:
        return None, None
    voucher, code = store.find_voucher_by_code(checkout.voucher_code)
    if voucher is None or code is None:
        return None, DomainError(ErrorCodes.CODE_REJECTED, "promoCode", "Promo code no longer exists.")

    user = store.users.get(checkout.user_id) if checkout.user_id is not None else None
    context = VoucherContext(
        channel,
        checkout.email,
        checkout.user_id,
        user is not None and user.is_staff and user.is_active,
        subtotal_base,
        quantity,
        checkout.shipping_address.country if checkout.shipping_address is not None else None,
    )
    error = validate_voucher(voucher, code, context, now)
    return (voucher, None) if error is None else (None, error)


def _apply_specific_product_voucher(store: Store, voucher: Voucher, channel: Channel, regular: list[_Draft]) -> None:
    listing = voucher.channel_listings[channel.id]
    currency = channel.currency
    eligible = [
        d
        for d in regular
        if catalogue_discounts.matches(store, voucher.selection, d.product, d.variant) and d.total > 0
    ]
    if not eligible:
        return

    def discount(d: _Draft, amount: Decimal) -> None:
        amount = min(amount, d.total)
        if amount <= 0:
            return
        d.total -= amount
        d.discounts.append(AppliedDiscount("VOUCHER", voucher.name, amount, "Promo code", voucher.id))

    if voucher.apply_once_per_order:
        # cheapest single unit across all lines
        cheapest = min(eligible, key=lambda d: d.catalogue_unit)
        discount(
            cheapest,
            amount_off(voucher.value_type, listing.discount_value, cheapest.catalogue_unit, currency),
        )
        return

    for d in eligible:
        if voucher.apply_once_per_line:
            amount = amount_off(voucher.value_type, listing.discount_value, d.catalogue_unit, currency)
        elif voucher.value_type == DiscountValueType.PERCENTAGE:
            amount = amount_off(voucher.value_type, listing.discount_value, d.total, currency)
        else:
            amount = min(d.total, listing.discount_value * d.line.quantity)
        discount(d, amount)


def _distribute_discount(
    regular: list[_Draft], amount: Decimal, currency: str, describe: Callable[[Decimal], AppliedDiscount]
) -> None:
    shares = distribute_proportionally(amount, [d.total for d in regular], currency)
    for d, share in zip(regular, shares, strict=True):
        if share <= 0:
            continue
        d.total -= share
        d.discounts.append(describe(share))


def _resolve_shipping(
    store: Store,
    checkout: Checkout,
    channel: Channel,
    subtotal_base: Decimal,
    weight: Decimal,
    requires_shipping: bool,
    drafts: list[_Draft],
) -> tuple[Decimal, uuid.UUID | None, bool]:
    if not requires_shipping:
        return ZERO, None, True
    delivery = checkout.delivery
    if delivery.kind == DeliveryKind.SHIPPING_METHOD:
        method = store.find_shipping_method(delivery.shipping_method_id)
        if method is not None:
            context = ShippingContext(
                channel,
                checkout.shipping_address,
                requires_shipping,
                subtotal_base,
                weight,
                frozenset(d.product.id for d in drafts),
            )
            if ShippingService.is_method_available(store, method.id, context):
                return method.channel_listings[channel.id].price, method.tax_class_id, True
            return ZERO, method.tax_class_id, False
    elif delivery.kind == DeliveryKind.EXTERNAL_SHIPPING_METHOD and delivery.external is not None:
        return delivery.external.price, delivery.external.tax_class_id, True
    elif delivery.kind == DeliveryKind.COLLECTION_POINT:
        return ZERO, None, True
    return ZERO, None, False


def _apply_gift_cards(
    store: Store, checkout: Checkout, currency: str, eligible: Decimal, now: datetime
) -> tuple[list[AppliedGiftCard], list[DomainError]]:
    applied: list[AppliedGiftCard] = []
    errors: list[DomainError] = []
    remaining = eligible
    for id_ in checkout.gift_card_ids:
        card = store.gift_cards.get(id_)
        if card is None or not card.is_usable_at(now, currency):
            errors.append(
                DomainError(
                    ErrorCodes.GIFT_CARD_REJECTED, "giftCards", f"Gift card {id_} can no longer be used.", (id_,)
                )
            )
            continue

        amount = min(card.current_balance, max(ZERO, remaining))
        remaining -= amount
        applied.append(AppliedGiftCard(card.id, card.code, amount))

    return applied, errors
