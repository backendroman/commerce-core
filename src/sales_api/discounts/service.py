from __future__ import annotations

import dataclasses
import secrets
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from ..common.clock import utc
from ..common.errors import ErrorCodes, ErrorCollector, ensure, must_exist
from ..common.money import Money, is_currency_code
from ..events import payloads
from ..events.domain_event import EventTypes
from ..notifications import NotificationType, Notifier
from ..permissions.model import Permission, Principal
from ..persistence.store import Store
from ..shop_context import ShopContext, require_permission
from .model import (
    DiscountValueType,
    GiftCard,
    GiftCardEvent,
    OrderCondition,
    Promotion,
    PromotionLogEntry,
    PromotionRule,
    PromotionType,
    Voucher,
    VoucherChannelListing,
    VoucherCode,
    VoucherType,
)
from .rules import is_valid_code_format, normalize_code

if TYPE_CHECKING:
    from ..catalog.indexer import CatalogIndexer


@dataclass(frozen=True, kw_only=True)
class PromotionRuleInput:
    name: str = ""
    channel_ids: tuple[uuid.UUID, ...] = ()
    product_ids: tuple[uuid.UUID, ...] = ()
    variant_ids: tuple[uuid.UUID, ...] = ()
    category_ids: tuple[uuid.UUID, ...] = ()
    collection_ids: tuple[uuid.UUID, ...] = ()
    condition: OrderCondition | None = None
    value_type: DiscountValueType | None = None
    value: Decimal | None = None
    gift_variant_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True, kw_only=True)
class PromotionInput:
    name: str
    type: PromotionType
    description: str = ""
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    rules: tuple[PromotionRuleInput, ...] = ()


@dataclass(frozen=True, kw_only=True)
class VoucherInput:
    name: str
    type: VoucherType
    codes: tuple[str, ...] = ()
    value_type: DiscountValueType = DiscountValueType.PERCENTAGE
    channel_listings: tuple[VoucherChannelListing, ...] = ()
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    product_ids: tuple[uuid.UUID, ...] = ()
    variant_ids: tuple[uuid.UUID, ...] = ()
    category_ids: tuple[uuid.UUID, ...] = ()
    collection_ids: tuple[uuid.UUID, ...] = ()
    countries: tuple[str, ...] = ()
    usage_limit: int | None = None
    apply_once_per_customer: bool = False
    single_use: bool = False
    apply_once_per_line: bool = False
    apply_once_per_order: bool = False
    min_checkout_items_quantity: int | None = None
    only_for_staff: bool = False


@dataclass(frozen=True, kw_only=True)
class GiftCardInput:
    balance: Decimal
    currency: str
    code: str | None = None
    expiry_date: date | None = None
    is_active: bool = True
    tags: tuple[str, ...] = ()
    owner_user_id: uuid.UUID | None = None


_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _random_code(length: int) -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(length))


def _code_taken(store: Store, code: str) -> bool:
    return store.find_voucher_by_code(code)[0] is not None or store.find_gift_card_by_code(code) is not None


class DiscountService:
    def __init__(self, ctx: ShopContext, indexer: CatalogIndexer, notifier: Notifier):
        self._ctx = ctx
        self._indexer = indexer
        self._notifier = notifier

    def create_promotion(self, principal: Principal, data: PromotionInput) -> Promotion:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_DISCOUNTS)

        def create(store: Store) -> Promotion:
            errors = ErrorCollector()
            if data.ends_at is not None and data.ends_at <= (data.starts_at or ctx.now):
                errors.add(ErrorCodes.INVALID, "endDate", "End must be after start.")
            for rule in data.rules:
                _validate_rule(store, data.type, rule, errors)
            errors.raise_if_any()

            promotion = Promotion(
                name=data.name,
                description=data.description,
                type=data.type,
                starts_at=utc(data.starts_at or ctx.now),
                ends_at=utc(data.ends_at) if data.ends_at is not None else None,
            )
            promotion.log.append(PromotionLogEntry(ctx.now, "CREATED", principal.user_id, principal.app_id))
            for r in data.rules:
                promotion.rules.append(_build_rule(r))
            if promotion.is_active_at(ctx.now):
                promotion.start_notified = True
                promotion.log.append(PromotionLogEntry(ctx.now, "STARTED", None, None))

            store.promotions[promotion.id] = promotion
            ctx.raise_event(EventTypes.PROMOTION_CREATED, payloads.promotion(promotion))
            if promotion.type == PromotionType.CATALOGUE:
                self._indexer.schedule_all()
            return promotion

        return ctx.db.write(create)

    def add_rule(self, principal: Principal, promotion_id: uuid.UUID, data: PromotionRuleInput) -> PromotionRule:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_DISCOUNTS)

        def add(store: Store) -> PromotionRule:
            promotion = must_exist(store.promotions.get(promotion_id), "promotion", promotion_id)
            errors = ErrorCollector()
            _validate_rule(store, promotion.type, data, errors)
            errors.raise_if_any()
            rule = _build_rule(data)
            promotion.rules.append(rule)
            promotion.log.append(
                PromotionLogEntry(ctx.now, "RULE_CREATED", principal.user_id, principal.app_id, rule.id)
            )
            ctx.raise_event(EventTypes.PROMOTION_UPDATED, payloads.promotion(promotion))
            if promotion.type == PromotionType.CATALOGUE:
                self._indexer.schedule_all()
            return rule

        return ctx.db.write(add)

    def delete_rule(self, principal: Principal, promotion_id: uuid.UUID, rule_id: uuid.UUID) -> None:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_DISCOUNTS)

        def delete(store: Store) -> None:
            promotion = must_exist(store.promotions.get(promotion_id), "promotion", promotion_id)
            before = len(promotion.rules)
            promotion.rules[:] = [r for r in promotion.rules if r.id != rule_id]
            ensure(before - len(promotion.rules) == 1, ErrorCodes.NOT_FOUND, "rule", "Rule not found.")
            promotion.log.append(
                PromotionLogEntry(ctx.now, "RULE_DELETED", principal.user_id, principal.app_id, rule_id)
            )
            ctx.raise_event(EventTypes.PROMOTION_UPDATED, payloads.promotion(promotion))
            if promotion.type == PromotionType.CATALOGUE:
                self._indexer.schedule_all()

        ctx.db.write(delete)

    def update_promotion_dates(
        self, principal: Principal, promotion_id: uuid.UUID, starts_at: datetime, ends_at: datetime | None
    ) -> None:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_DISCOUNTS)

        def update(store: Store) -> None:
            promotion = must_exist(store.promotions.get(promotion_id), "promotion", promotion_id)
            ensure(ends_at is None or ends_at > starts_at, ErrorCodes.INVALID, "endDate", "End must be after start.")
            promotion.starts_at = utc(starts_at)
            promotion.ends_at = utc(ends_at) if ends_at is not None else None
            promotion.start_notified = promotion.starts_at <= ctx.now and promotion.start_notified
            promotion.end_notified = False
            promotion.log.append(PromotionLogEntry(ctx.now, "UPDATED", principal.user_id, principal.app_id))
            ctx.raise_event(EventTypes.PROMOTION_UPDATED, payloads.promotion(promotion))
            if promotion.type == PromotionType.CATALOGUE:
                self._indexer.schedule_all()

        ctx.db.write(update)

    def process_promotion_schedule(self) -> None:
        ctx = self._ctx

        def process(store: Store) -> bool:
            changed = False
            for p in store.promotions.values():
                if not p.start_notified and p.starts_at <= ctx.now:
                    p.start_notified = True
                    p.log.append(PromotionLogEntry(ctx.now, "STARTED", None, None))
                    ctx.raise_event(EventTypes.PROMOTION_STARTED, payloads.promotion(p))
                    changed |= p.type == PromotionType.CATALOGUE

                if not p.end_notified and p.ends_at is not None and p.ends_at <= ctx.now:
                    p.end_notified = True
                    p.log.append(PromotionLogEntry(ctx.now, "ENDED", None, None))
                    ctx.raise_event(EventTypes.PROMOTION_ENDED, payloads.promotion(p))
                    changed |= p.type == PromotionType.CATALOGUE
            return changed

        if ctx.db.write(process):
            self._indexer.schedule_all()

    def create_voucher(self, principal: Principal, data: VoucherInput) -> Voucher:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_DISCOUNTS)

        def create(store: Store) -> Voucher:
            errors = ErrorCollector()
            codes = [normalize_code(c) for c in data.codes]
            if not codes:
                errors.add(ErrorCodes.REQUIRED, "codes", "At least one code is required.")
            for code in codes:
                if not is_valid_code_format(code):
                    errors.add(ErrorCodes.INVALID, "codes", f"'{code}' must be 4-32 latin letters, digits or dashes.")
                if _code_taken(store, code):
                    errors.add(ErrorCodes.DUPLICATE, "codes", f"Code '{code}' already exists.")

            if len(set(codes)) != len(codes):
                errors.add(ErrorCodes.DUPLICATE, "codes", "Codes must be unique.")
            for listing in data.channel_listings:
                if listing.channel_id not in store.channels:
                    errors.add(ErrorCodes.NOT_FOUND, "channelListings", f"Channel {listing.channel_id} not found.")
                if listing.discount_value < 0 or (
                    data.value_type == DiscountValueType.PERCENTAGE and listing.discount_value > 100
                ):
                    errors.add(ErrorCodes.INVALID, "discountValue", "Invalid discount value.")

            if data.apply_once_per_order and data.type not in (VoucherType.SPECIFIC_PRODUCT, VoucherType.ENTIRE_ORDER):
                errors.add(ErrorCodes.INVALID, "applyOncePerOrder", "Not applicable to shipping promo codes.")
            errors.raise_if_any()

            voucher = Voucher(
                name=data.name,
                type=data.type,
                value_type=data.value_type,
                starts_at=utc(data.starts_at or ctx.now),
                ends_at=utc(data.ends_at) if data.ends_at is not None else None,
                usage_limit=data.usage_limit,
                apply_once_per_customer=data.apply_once_per_customer,
                single_use=data.single_use,
                apply_once_per_line=data.apply_once_per_line,
                apply_once_per_order=data.apply_once_per_order,
                min_checkout_items_quantity=data.min_checkout_items_quantity,
                only_for_staff=data.only_for_staff,
            )
            voucher.codes.extend(VoucherCode(code=c) for c in codes)
            for listing in data.channel_listings:
                voucher.channel_listings[listing.channel_id] = listing
            voucher.selection.product_ids.update(data.product_ids)
            voucher.selection.variant_ids.update(data.variant_ids)
            voucher.selection.category_ids.update(data.category_ids)
            voucher.selection.collection_ids.update(data.collection_ids)
            voucher.countries.update(data.countries)
            store.vouchers.add(voucher.id, voucher)
            ctx.raise_event(EventTypes.VOUCHER_CREATED, payloads.ref(voucher.id, voucher.name))
            return voucher

        return ctx.db.write(create)

    def generate_codes(self, principal: Principal, voucher_id: uuid.UUID, quantity: int, prefix: str = "") -> list[str]:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_DISCOUNTS)
        ensure(0 < quantity <= 1000, ErrorCodes.INVALID, "quantity", "Quantity must be between 1 and 1000.")

        def generate(store: Store) -> list[str]:
            voucher = must_exist(store.vouchers.get(voucher_id), "voucher", voucher_id)
            normalized_prefix = normalize_code(prefix)
            ensure(len(normalized_prefix) <= 20, ErrorCodes.INVALID, "prefix", "Prefix is too long.")
            codes: list[str] = []
            while len(codes) < quantity:
                code = (normalized_prefix + "-" if normalized_prefix else "") + _random_code(8)
                if _code_taken(store, code) or code in codes:
                    continue
                codes.append(code)

            store.vouchers.add_codes(voucher, [VoucherCode(code=c) for c in codes])
            ctx.raise_event(EventTypes.VOUCHER_CODES_CREATED, payloads.ref(voucher.id, voucher.name))
            return codes

        return ctx.db.write(generate)

    def issue_gift_card(self, principal: Principal, data: GiftCardInput) -> GiftCard:
        return self._ctx.db.write(lambda store: self._issue_gift_card(principal, store, data))

    def issue_gift_cards(self, principal: Principal, count: int, template: GiftCardInput) -> list[GiftCard]:
        ensure(0 < count <= 1000, ErrorCodes.INVALID, "count", "Count must be between 1 and 1000.")
        return self._ctx.db.write(
            lambda store: [
                self._issue_gift_card(principal, store, dataclasses.replace(template, code=None)) for _ in range(count)
            ]
        )

    def _issue_gift_card(self, principal: Principal, store: Store, data: GiftCardInput) -> GiftCard:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_GIFT_CARDS)
        errors = ErrorCollector()
        if data.balance <= 0:
            errors.add(ErrorCodes.INVALID, "balance", "Balance must be positive.")
        if not is_currency_code(data.currency):
            errors.add(ErrorCodes.INVALID, "currency", "Invalid currency.")
        code = None if data.code is None else normalize_code(data.code)
        if code is not None and (not is_valid_code_format(code) or _code_taken(store, code)):
            errors.add(ErrorCodes.DUPLICATE, "code", "Code is invalid or taken.")
        if data.expiry_date is not None and data.expiry_date < ctx.now.date():
            errors.add(ErrorCodes.INVALID, "expiryDate", "Expiry date is in the past.")
        errors.raise_if_any()

        while code is None or _code_taken(store, code):
            code = _random_code(12)
        card = GiftCard(
            code=code,
            currency=data.currency,
            initial_balance=data.balance,
            current_balance=data.balance,
            expiry_date=data.expiry_date,
            is_active=data.is_active,
            owner_user_id=data.owner_user_id,
            created_by_user_id=principal.user_id,
            created_by_app_id=principal.app_id,
            created_at=ctx.now,
        )
        card.tags.update(data.tags)
        card.events.append(GiftCardEvent(ctx.now, "ISSUED", data.balance, None, principal.user_id))
        store.gift_cards.add(card.id, card)
        ctx.raise_event(EventTypes.GIFT_CARD_CREATED, payloads.gift_card(card))
        return card

    def set_gift_card_active(self, principal: Principal, id_: uuid.UUID, active: bool) -> None:
        def change(card: GiftCard) -> None:
            card.is_active = active
            card.events.append(
                GiftCardEvent(self._ctx.now, "ACTIVATED" if active else "DEACTIVATED", None, None, principal.user_id)
            )
            self._ctx.raise_event(EventTypes.GIFT_CARD_STATUS_CHANGED, payloads.gift_card(card))

        self._update_gift_card(principal, id_, change)

    def set_gift_card_balance(self, principal: Principal, id_: uuid.UUID, balance: Decimal) -> None:
        def change(card: GiftCard) -> None:
            ensure(balance >= 0, ErrorCodes.INVALID, "balance", "Balance cannot be negative.")
            card.events.append(
                GiftCardEvent(self._ctx.now, "BALANCE_SET", balance - card.current_balance, None, principal.user_id)
            )
            card.current_balance = balance

        self._update_gift_card(principal, id_, change)

    def set_gift_card_expiry(self, principal: Principal, id_: uuid.UUID, expiry: date | None) -> None:
        def change(card: GiftCard) -> None:
            card.expiry_date = expiry
            card.events.append(GiftCardEvent(self._ctx.now, "EXPIRY_CHANGED", None, None, principal.user_id))

        self._update_gift_card(principal, id_, change)

    def set_gift_card_tags(self, principal: Principal, id_: uuid.UUID, tags: Sequence[str]) -> None:
        def change(card: GiftCard) -> None:
            card.tags.clear()
            card.tags.update(tags)
            card.events.append(GiftCardEvent(self._ctx.now, "TAGS_UPDATED", None, None, principal.user_id))

        self._update_gift_card(principal, id_, change)

    def delete_gift_card(self, principal: Principal, id_: uuid.UUID) -> None:
        require_permission(principal, Permission.MANAGE_GIFT_CARDS)

        def delete(store: Store) -> None:
            must_exist(store.gift_cards.get(id_), "id", id_)
            store.gift_cards.remove(id_)
            for checkout in store.checkouts.values():
                if id_ in checkout.gift_card_ids:
                    checkout.gift_card_ids.remove(id_)

        self._ctx.db.write(delete)

    def send_gift_card(
        self, principal: Principal, id_: uuid.UUID, email: str, language_code: str | None = None
    ) -> None:
        sent: list[GiftCard] = []

        def change(c: GiftCard) -> None:
            sent.append(c)
            c.events.append(GiftCardEvent(self._ctx.now, "SENT_TO_CUSTOMER", None, None, principal.user_id, email))
            self._ctx.raise_event(EventTypes.GIFT_CARD_SENT, payloads.gift_card(c))

        self._update_gift_card(principal, id_, change)
        card = sent[0]
        self._notifier.send(
            NotificationType.GIFT_CARD_SENT,
            email,
            language_code,
            {"code": card.code, "balance": str(Money(card.current_balance, card.currency))},
        )

    def _update_gift_card(self, principal: Principal, id_: uuid.UUID, change: Callable[[GiftCard], None]) -> None:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_GIFT_CARDS)

        def update(store: Store) -> None:
            card = must_exist(store.gift_cards.get(id_), "id", id_)
            change(card)
            ctx.raise_event(EventTypes.GIFT_CARD_UPDATED, payloads.gift_card(card))

        ctx.db.write(update)


def _build_rule(r: PromotionRuleInput) -> PromotionRule:
    rule = PromotionRule(name=r.name, condition=r.condition, value_type=r.value_type, value=r.value)
    rule.channel_ids.update(r.channel_ids)
    rule.selection.product_ids.update(r.product_ids)
    rule.selection.variant_ids.update(r.variant_ids)
    rule.selection.category_ids.update(r.category_ids)
    rule.selection.collection_ids.update(r.collection_ids)
    rule.gift_variant_ids.extend(dict.fromkeys(r.gift_variant_ids))
    return rule


def _validate_rule(store: Store, kind: PromotionType, r: PromotionRuleInput, errors: ErrorCollector) -> None:
    if not r.channel_ids:
        errors.add(ErrorCodes.REQUIRED, "channels", "A rule needs at least one channel.")
    for id_ in r.channel_ids:
        if id_ not in store.channels:
            errors.add(ErrorCodes.NOT_FOUND, "channels", f"Channel {id_} not found.")
    currencies = len({store.channels[id_].currency for id_ in r.channel_ids if id_ in store.channels})
    has_discount = r.value_type is not None and r.value is not None
    if r.value is not None and r.value < 0:
        errors.add(ErrorCodes.INVALID, "rewardValue", "Value cannot be negative.")
    if r.value_type == DiscountValueType.PERCENTAGE and r.value is not None and r.value > 100:
        errors.add(ErrorCodes.INVALID, "rewardValue", "Percentage cannot exceed 100.")
    if r.value_type == DiscountValueType.FIXED and currencies > 1:
        errors.add(ErrorCodes.INVALID, "channels", "Fixed discounts require channels with one currency.")

    if kind == PromotionType.CATALOGUE:
        if not has_discount:
            errors.add(ErrorCodes.REQUIRED, "rewardValue", "Catalogue rules need a discount.")
        if not (r.product_ids or r.variant_ids or r.category_ids or r.collection_ids):
            errors.add(
                ErrorCodes.REQUIRED,
                "cataloguePredicate",
                "Catalogue rules need products, variants, categories or collections.",
            )
        if r.condition is not None or r.gift_variant_ids:
            errors.add(ErrorCodes.INVALID, "orderPredicate", "Catalogue rules can't have order conditions or gifts.")
    else:
        if r.condition is None:
            errors.add(ErrorCodes.REQUIRED, "orderPredicate", "Order rules need a condition.")
        if has_discount == (len(r.gift_variant_ids) > 0):
            errors.add(ErrorCodes.INVALID, "reward", "Order rules need either a discount or gifts.")
        if currencies > 1:
            errors.add(ErrorCodes.INVALID, "channels", "Order thresholds require channels with one currency.")
        for id_ in r.gift_variant_ids:
            if id_ not in store.variants:
                errors.add(ErrorCodes.NOT_FOUND, "gifts", f"Variant {id_} not found.")
