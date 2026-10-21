from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from ..common.address import Address
from ..common.money import TaxedMoney
from ..payments.model import AuthorizeStatus, ChargeStatus, PaymentMethodDetails, Transaction
from .model import AppliedGiftCard, Order, OrderDelivery, OrderLine, OrderStatus


@dataclass(frozen=True)
class TransactionView:
    id: uuid.UUID
    name: str
    provider_reference: str | None
    authorized: Decimal
    charged: Decimal
    payment_method: PaymentMethodDetails | None


@dataclass(frozen=True, kw_only=True)
class OrderView:
    id: uuid.UUID
    number: int
    created_at: datetime
    status: OrderStatus
    email: str
    lines: tuple[OrderLine, ...]
    subtotal: TaxedMoney
    shipping_price: TaxedMoney
    total: TaxedMoney
    shipping_address: Address | None = None
    billing_address: Address | None = None
    delivery: OrderDelivery | None = None
    charge_status: ChargeStatus = ChargeStatus.NONE
    authorize_status: AuthorizeStatus = AuthorizeStatus.NONE
    is_masked: bool = False
    transactions: tuple[TransactionView, ...] | None = None
    voucher_code: str | None = None
    gift_cards: tuple[AppliedGiftCard, ...] | None = None
    customer_note: str | None = None
    metadata: Mapping[str, str] | None = None

    @staticmethod
    def full(order: Order, transactions: Iterable[Transaction]) -> OrderView:
        return OrderView(
            id=order.id,
            number=order.number,
            created_at=order.created_at,
            status=order.status,
            email=order.email,
            shipping_address=order.shipping_address,
            billing_address=order.billing_address,
            lines=tuple(order.lines),
            subtotal=order.subtotal,
            shipping_price=order.shipping_price,
            total=order.total,
            delivery=order.delivery,
            charge_status=order.charge_status,
            authorize_status=order.authorize_status,
            transactions=tuple(
                TransactionView(
                    t.id, t.name, t.provider_reference, t.authorized_amount, t.charged_amount, t.payment_method
                )
                for t in transactions
            ),
            voucher_code=order.voucher_code,
            gift_cards=tuple(order.gift_cards),
            customer_note=order.customer_note,
            metadata=dict(order.metadata),
        )

    @staticmethod
    def masked(order: Order) -> OrderView:
        return OrderView(
            id=order.id,
            number=order.number,
            created_at=order.created_at,
            status=order.status,
            email=mask_email(order.email),
            shipping_address=mask_address(order.shipping_address),
            billing_address=mask_address(order.billing_address),
            lines=tuple(order.lines),
            subtotal=order.subtotal,
            shipping_price=order.shipping_price,
            total=order.total,
            delivery=order.delivery,
            charge_status=order.charge_status,
            authorize_status=order.authorize_status,
            is_masked=True,
        )


def mask_value(value: str) -> str:
    return value if len(value) <= 1 else value[0] + "*" * (len(value) - 1)


def mask_email(email: str) -> str:
    at = email.find("@")
    return mask_value(email) if at <= 0 else mask_value(email[:at]) + email[at:]


def mask_address(a: Address | None) -> Address | None:
    if a is None:
        return None
    v = mask_value
    return dataclasses.replace(
        a,
        first_name=v(a.first_name),
        last_name=v(a.last_name),
        company_name=v(a.company_name),
        street_address_1=v(a.street_address_1),
        street_address_2=v(a.street_address_2),
        city=v(a.city),
        city_area=v(a.city_area),
        postal_code=v(a.postal_code),
        country_area=v(a.country_area),
        phone=v(a.phone),
        metadata={},
    )
