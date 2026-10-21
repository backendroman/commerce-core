from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum, auto

from ..common.address import Address
from ..common.money import ZERO, Money, TaxedMoney, dsum
from ..payments.model import AuthorizeStatus, ChargeStatus


class OrderStatus(StrEnum):
    UNCONFIRMED = auto()
    UNFULFILLED = auto()
    PARTIALLY_FULFILLED = auto()
    FULFILLED = auto()
    CANCELED = auto()
    RETURNED = auto()


@dataclass(frozen=True)
class AppliedDiscount:
    type: str
    name: str
    amount: Decimal
    reason: str
    source_id: uuid.UUID | None


@dataclass(frozen=True, kw_only=True)
class OrderLine:
    variant_id: uuid.UUID
    product_name: str
    variant_name: str
    undiscounted_unit_price: Money
    unit_price: TaxedMoney
    undiscounted_total: TaxedMoney
    total: TaxedMoney
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    sku: str | None = None
    quantity: int = 0
    is_gift: bool = False
    is_shipping_required: bool = False
    is_gift_card: bool = False
    tax_rate: Decimal = ZERO
    discounts: tuple[AppliedDiscount, ...] = ()


@dataclass(frozen=True)
class OrderDelivery:
    kind: str
    method_id: str | None
    name: str
    price: TaxedMoney
    warehouse_id: uuid.UUID | None


@dataclass(frozen=True)
class AppliedGiftCard:
    gift_card_id: uuid.UUID
    code: str
    amount: Decimal


@dataclass(eq=False, kw_only=True)
class Order:
    channel_id: uuid.UUID
    currency: str
    email: str
    subtotal: TaxedMoney
    shipping_price: TaxedMoney
    total: TaxedMoney
    undiscounted_total: TaxedMoney
    checkout_token: uuid.UUID
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    number: int = 0
    status: OrderStatus = OrderStatus.UNCONFIRMED
    language_code: str = "en"
    user_id: uuid.UUID | None = None
    shipping_address: Address | None = None
    billing_address: Address | None = None
    delivery: OrderDelivery | None = None
    lines: list[OrderLine] = field(default_factory=list)
    voucher_id: uuid.UUID | None = None
    voucher_code: str | None = None
    gift_cards: list[AppliedGiftCard] = field(default_factory=list)
    discounts: tuple[AppliedDiscount, ...] = ()
    customer_note: str | None = None
    charge_status: ChargeStatus = ChargeStatus.NONE
    authorize_status: AuthorizeStatus = AuthorizeStatus.NONE
    is_overpaid_at_checkout: bool = False
    total_charged: Decimal = ZERO
    total_authorized: Decimal = ZERO
    tax_exempt: bool = False
    created_at: datetime
    metadata: dict[str, str] = field(default_factory=dict)
    private_metadata: dict[str, str] = field(default_factory=dict)

    @property
    def gift_cards_total(self) -> Decimal:
        return dsum(g.amount for g in self.gift_cards)
