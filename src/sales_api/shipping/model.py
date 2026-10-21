from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum, auto

from ..common.case_insensitive import CaseInsensitiveDict, CaseInsensitiveSet


@dataclass(eq=False, kw_only=True)
class ShippingZone:
    name: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    countries: CaseInsensitiveSet = field(default_factory=CaseInsensitiveSet)
    is_default: bool = False
    warehouse_ids: set[uuid.UUID] = field(default_factory=set)
    methods: list[ShippingMethod] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)
    private_metadata: dict[str, str] = field(default_factory=dict)


class ShippingMethodType(StrEnum):
    PRICE = auto()
    WEIGHT = auto()


class PostalCodeRuleMode(StrEnum):
    INCLUDE = auto()
    EXCLUDE = auto()


def _normalize_postal_code(s: str) -> str:
    return s.replace(" ", "").upper()


@dataclass(frozen=True)
class PostalCodeRange:
    start: str
    end: str | None = None

    def contains(self, postal_code: str) -> bool:
        code = _normalize_postal_code(postal_code)
        start = _normalize_postal_code(self.start)
        if self.end is None:
            return code == start
        end = _normalize_postal_code(self.end)
        return len(code) == len(start) and start <= code <= end


@dataclass(frozen=True)
class ShippingMethodChannelListing:
    channel_id: uuid.UUID
    price: Decimal
    minimum_order_price: Decimal | None = None
    maximum_order_price: Decimal | None = None


@dataclass(eq=False, kw_only=True)
class ShippingMethod:
    shipping_zone_id: uuid.UUID
    name: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    description: str = ""
    type: ShippingMethodType = ShippingMethodType.PRICE
    channel_listings: dict[uuid.UUID, ShippingMethodChannelListing] = field(default_factory=dict)
    minimum_weight_kg: Decimal | None = None
    maximum_weight_kg: Decimal | None = None
    minimum_delivery_days: int | None = None
    maximum_delivery_days: int | None = None
    tax_class_id: uuid.UUID | None = None
    postal_code_ranges: list[PostalCodeRange] = field(default_factory=list)
    postal_code_mode: PostalCodeRuleMode = PostalCodeRuleMode.EXCLUDE
    excluded_product_ids: set[uuid.UUID] = field(default_factory=set)
    translations: CaseInsensitiveDict[str] = field(default_factory=CaseInsensitiveDict)
    metadata: dict[str, str] = field(default_factory=dict)
    private_metadata: dict[str, str] = field(default_factory=dict)

    def postal_code_allowed(self, postal_code: str) -> bool:
        if not self.postal_code_ranges:
            return True
        in_range = any(r.contains(postal_code) for r in self.postal_code_ranges)
        return in_range if self.postal_code_mode == PostalCodeRuleMode.INCLUDE else not in_range
