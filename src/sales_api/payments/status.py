from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum, auto
from typing import TYPE_CHECKING

from ..common.money import dsum
from .model import AuthorizeStatus, ChargeStatus, LegacyChargeStatus, LegacyPayment

if TYPE_CHECKING:
    from ..persistence.store import Store

_INACTIVE_LEGACY = (LegacyChargeStatus.CANCELLED, LegacyChargeStatus.REFUSED)


class CheckoutPaymentMechanism(StrEnum):
    TRANSACTIONS = auto()
    LEGACY_PAYMENT = auto()


@dataclass(frozen=True)
class PaymentSummary:
    mechanism: CheckoutPaymentMechanism
    active_legacy_payment: LegacyPayment | None
    charged: Decimal
    authorized: Decimal
    charge_status: ChargeStatus
    authorize_status: AuthorizeStatus

    @property
    def covered(self) -> Decimal:
        return self.charged + self.authorized


def active_legacy_payment(store: Store, checkout_id: uuid.UUID) -> LegacyPayment | None:
    candidates = [
        p
        for p in store.legacy_payments.values()
        if p.checkout_id == checkout_id and p.is_active and p.status not in _INACTIVE_LEGACY
    ]
    candidates.sort(key=lambda p: p.created_at, reverse=True)
    return candidates[0] if candidates else None


def for_checkout(store: Store, checkout_id: uuid.UUID, amount_to_pay: Decimal) -> PaymentSummary:
    legacy = active_legacy_payment(store, checkout_id)
    if legacy is not None and legacy.covered >= amount_to_pay and amount_to_pay > 0:
        return _summary(
            CheckoutPaymentMechanism.LEGACY_PAYMENT,
            legacy,
            legacy.captured_amount,
            legacy.authorized_amount,
            amount_to_pay,
        )

    transactions = [t for t in store.transactions.values() if t.checkout_id == checkout_id]
    return _summary(
        CheckoutPaymentMechanism.TRANSACTIONS,
        legacy,
        dsum(t.charged_amount for t in transactions),
        dsum(t.authorized_amount for t in transactions),
        amount_to_pay,
    )


def for_order(store: Store, order_id: uuid.UUID, amount_to_pay: Decimal) -> PaymentSummary:
    transactions = [t for t in store.transactions.values() if t.order_id == order_id]
    legacy = [p for p in store.legacy_payments.values() if p.order_id == order_id]
    return _summary(
        CheckoutPaymentMechanism.TRANSACTIONS,
        None,
        dsum(t.charged_amount for t in transactions) + dsum(p.captured_amount for p in legacy),
        dsum(t.authorized_amount for t in transactions) + dsum(p.authorized_amount for p in legacy),
        amount_to_pay,
    )


def total_covered_by_all_mechanisms(store: Store, checkout_id: uuid.UUID) -> Decimal:
    return dsum(
        t.charged_amount + t.authorized_amount for t in store.transactions.values() if t.checkout_id == checkout_id
    ) + dsum(
        p.covered
        for p in store.legacy_payments.values()
        if p.checkout_id == checkout_id and p.status not in _INACTIVE_LEGACY
    )


def _summary(
    mechanism: CheckoutPaymentMechanism,
    legacy: LegacyPayment | None,
    charged: Decimal,
    authorized: Decimal,
    amount_to_pay: Decimal,
) -> PaymentSummary:
    if charged <= 0:
        charge_status = ChargeStatus.FULL if amount_to_pay == 0 else ChargeStatus.NONE
    elif charged < amount_to_pay:
        charge_status = ChargeStatus.PARTIAL
    elif charged == amount_to_pay:
        charge_status = ChargeStatus.FULL
    else:
        charge_status = ChargeStatus.OVERCHARGED

    covered = charged + authorized
    if covered <= 0:
        authorize_status = AuthorizeStatus.FULL if amount_to_pay == 0 else AuthorizeStatus.NONE
    elif covered < amount_to_pay:
        authorize_status = AuthorizeStatus.PARTIAL
    else:
        authorize_status = AuthorizeStatus.FULL
    return PaymentSummary(mechanism, legacy, charged, authorized, charge_status, authorize_status)
