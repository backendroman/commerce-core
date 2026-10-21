from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum, auto

from ..common.case_insensitive import CaseInsensitiveDict, CaseInsensitiveSet


class PromotionType(StrEnum):
    CATALOGUE = auto()
    ORDER = auto()


class DiscountValueType(StrEnum):
    PERCENTAGE = auto()
    FIXED = auto()


@dataclass(frozen=True)
class PromotionLogEntry:
    at: datetime
    type: str
    author_user_id: uuid.UUID | None
    author_app_id: uuid.UUID | None
    rule_id: uuid.UUID | None = None


@dataclass(eq=False, kw_only=True)
class Promotion:
    name: str
    type: PromotionType
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    description: str = ""
    starts_at: datetime
    ends_at: datetime | None = None
    rules: list[PromotionRule] = field(default_factory=list)
    log: list[PromotionLogEntry] = field(default_factory=list)
    translations: CaseInsensitiveDict[str] = field(default_factory=CaseInsensitiveDict)
    metadata: dict[str, str] = field(default_factory=dict)
    private_metadata: dict[str, str] = field(default_factory=dict)

    start_notified: bool = False
    end_notified: bool = False

    def is_active_at(self, now: datetime) -> bool:
        return self.starts_at is not None and self.starts_at <= now and (self.ends_at is None or self.ends_at > now)


@dataclass(eq=False)
class CatalogueSelection:
    product_ids: set[uuid.UUID] = field(default_factory=set)
    variant_ids: set[uuid.UUID] = field(default_factory=set)
    category_ids: set[uuid.UUID] = field(default_factory=set)
    collection_ids: set[uuid.UUID] = field(default_factory=set)

    @property
    def is_empty(self) -> bool:
        return not (self.product_ids or self.variant_ids or self.category_ids or self.collection_ids)


class ComparisonOperator(StrEnum):
    GT = auto()
    GTE = auto()
    LT = auto()
    LTE = auto()
    EQ = auto()


class OrderCondition:
    def is_satisfied(self, subtotal: Decimal) -> bool:
        raise NotImplementedError


@dataclass(frozen=True)
class SubtotalCondition(OrderCondition):
    operator: ComparisonOperator
    threshold: Decimal

    def is_satisfied(self, subtotal: Decimal) -> bool:
        match self.operator:
            case ComparisonOperator.GT:
                return subtotal > self.threshold
            case ComparisonOperator.GTE:
                return subtotal >= self.threshold
            case ComparisonOperator.LT:
                return subtotal < self.threshold
            case ComparisonOperator.LTE:
                return subtotal <= self.threshold
            case ComparisonOperator.EQ:
                return subtotal == self.threshold
        return False


@dataclass(frozen=True)
class AndCondition(OrderCondition):
    parts: tuple[OrderCondition, ...]

    def is_satisfied(self, subtotal: Decimal) -> bool:
        return all(p.is_satisfied(subtotal) for p in self.parts)


@dataclass(frozen=True)
class OrCondition(OrderCondition):
    parts: tuple[OrderCondition, ...]

    def is_satisfied(self, subtotal: Decimal) -> bool:
        return any(p.is_satisfied(subtotal) for p in self.parts)


@dataclass(eq=False, kw_only=True)
class PromotionRule:
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    name: str = ""
    channel_ids: set[uuid.UUID] = field(default_factory=set)

    # catalogue rules
    selection: CatalogueSelection = field(default_factory=CatalogueSelection)

    # order rules
    condition: OrderCondition | None = None

    value_type: DiscountValueType | None = None
    value: Decimal | None = None

    gift_variant_ids: list[uuid.UUID] = field(default_factory=list)

    translations: CaseInsensitiveDict[str] = field(default_factory=CaseInsensitiveDict)

    @property
    def is_gift_reward(self) -> bool:
        return len(self.gift_variant_ids) > 0


class VoucherType(StrEnum):
    ENTIRE_ORDER = auto()
    SPECIFIC_PRODUCT = auto()
    SHIPPING = auto()


@dataclass(frozen=True)
class VoucherChannelListing:
    channel_id: uuid.UUID
    discount_value: Decimal
    min_spent: Decimal | None = None


@dataclass(eq=False, kw_only=True)
class VoucherCode:
    code: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    used: int = 0
    is_active: bool = True


@dataclass(eq=False, kw_only=True)
class Voucher:
    name: str
    type: VoucherType
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    codes: list[VoucherCode] = field(default_factory=list)
    starts_at: datetime
    ends_at: datetime | None = None
    value_type: DiscountValueType = DiscountValueType.PERCENTAGE
    channel_listings: dict[uuid.UUID, VoucherChannelListing] = field(default_factory=dict)
    selection: CatalogueSelection = field(default_factory=CatalogueSelection)
    countries: CaseInsensitiveSet = field(default_factory=CaseInsensitiveSet)

    usage_limit: int | None = None
    apply_once_per_customer: bool = False
    single_use: bool = False
    apply_once_per_line: bool = False
    apply_once_per_order: bool = False
    min_checkout_items_quantity: int | None = None
    only_for_staff: bool = False

    customer_emails_used: CaseInsensitiveSet = field(default_factory=CaseInsensitiveSet)

    translations: CaseInsensitiveDict[str] = field(default_factory=CaseInsensitiveDict)
    metadata: dict[str, str] = field(default_factory=dict)
    private_metadata: dict[str, str] = field(default_factory=dict)

    @property
    def total_used(self) -> int:
        return sum(c.used for c in self.codes)

    def is_active_at(self, now: datetime) -> bool:
        return self.starts_at is not None and self.starts_at <= now and (self.ends_at is None or self.ends_at > now)


@dataclass(frozen=True)
class GiftCardEvent:
    at: datetime
    type: str
    balance_delta: Decimal | None
    order_id: uuid.UUID | None
    user_id: uuid.UUID | None
    message: str | None = None


@dataclass(eq=False, kw_only=True)
class GiftCard:
    code: str
    currency: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    initial_balance: Decimal = Decimal(0)
    current_balance: Decimal = Decimal(0)
    expiry_date: date | None = None
    is_active: bool = True
    tags: CaseInsensitiveSet = field(default_factory=CaseInsensitiveSet)
    owner_user_id: uuid.UUID | None = None
    created_by_user_id: uuid.UUID | None = None
    created_by_app_id: uuid.UUID | None = None
    created_at: datetime
    events: list[GiftCardEvent] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    private_metadata: dict[str, str] = field(default_factory=dict)

    def is_expired_at(self, now: datetime) -> bool:
        return self.expiry_date is not None and now.date() > self.expiry_date

    def is_usable_at(self, now: datetime, currency: str) -> bool:
        return self.is_active and not self.is_expired_at(now) and self.currency == currency and self.current_balance > 0
