from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum, auto

from ..common.address import Address


@dataclass(eq=False, kw_only=True)
class CheckoutLine:
    variant_id: uuid.UUID
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    quantity: int = 0
    price_override: Decimal | None = None
    is_gift: bool = False
    gift_promotion_rule_id: uuid.UUID | None = None
    created_at: datetime
    metadata: dict[str, str] = field(default_factory=dict)
    private_metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ExternalShippingMethod:
    app_id: uuid.UUID
    id: str
    name: str
    price: Decimal
    tax_class_id: uuid.UUID | None = None


class DeliveryKind(StrEnum):
    NONE = auto()
    SHIPPING_METHOD = auto()
    EXTERNAL_SHIPPING_METHOD = auto()
    COLLECTION_POINT = auto()


@dataclass(frozen=True, kw_only=True)
class DeliverySelection:
    kind: DeliveryKind = DeliveryKind.NONE
    shipping_method_id: uuid.UUID | None = None
    external: ExternalShippingMethod | None = None
    collection_point_warehouse_id: uuid.UUID | None = None


NO_DELIVERY = DeliverySelection(kind=DeliveryKind.NONE)


@dataclass(eq=False, kw_only=True)
class Checkout:
    channel_id: uuid.UUID
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    token: uuid.UUID = field(default_factory=uuid.uuid4)
    user_id: uuid.UUID | None = None
    email: str | None = None
    language_code: str = "en"
    shipping_address: Address | None = None
    billing_address: Address | None = None
    delivery: DeliverySelection = NO_DELIVERY
    voucher_code: str | None = None
    gift_card_ids: list[uuid.UUID] = field(default_factory=list)
    lines: list[CheckoutLine] = field(default_factory=list)
    tax_exempt: bool = False
    customer_note: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    private_metadata: dict[str, str] = field(default_factory=dict)
    created_at: datetime
    last_change_at: datetime

    version: int = 0

    preferred_gift_variant_id: uuid.UUID | None = None

    auto_complete_attempts: int = 0
    next_auto_complete_attempt_at: datetime | None = None
    fully_paid_event_sent: bool = False

    def touch(self, now: datetime) -> None:
        self.last_change_at = now
        self.version += 1
