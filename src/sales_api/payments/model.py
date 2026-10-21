from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum, auto

from ..common.money import ZERO


class TransactionEventType(StrEnum):
    HOLD_REQUESTED = auto()
    HOLD_PLACED = auto()
    HOLD_DECLINED = auto()
    HOLD_NEEDS_ACTION = auto()
    CHARGE_REQUESTED = auto()
    CHARGE_SETTLED = auto()
    CHARGE_DECLINED = auto()
    CHARGE_NEEDS_ACTION = auto()
    NOTE = auto()


class EventSource(StrEnum):
    APP = auto()
    USER = auto()
    SYSTEM = auto()


@dataclass(frozen=True, kw_only=True)
class TransactionEvent:
    type: TransactionEventType
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    amount: Decimal = ZERO
    message: str = ""
    external_url: str | None = None
    provider_reference: str | None = None
    created_at: datetime
    source: EventSource = EventSource.APP
    app_id: uuid.UUID | None = None


@dataclass(frozen=True)
class PaymentMethodDetails:
    type: str
    name: str | None = None
    brand: str | None = None
    last_digits: str | None = None
    exp_month: int | None = None
    exp_year: int | None = None


@dataclass(eq=False, kw_only=True)
class Transaction:
    currency: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    token: uuid.UUID = field(default_factory=uuid.uuid4)
    checkout_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    app_id: uuid.UUID | None = None
    name: str = ""
    provider_reference: str | None = None
    external_url: str | None = None
    payment_method: PaymentMethodDetails | None = None
    idempotency_key: str | None = None
    created_at: datetime
    events: list[TransactionEvent] = field(default_factory=list)

    authorized_amount: Decimal = ZERO
    charged_amount: Decimal = ZERO
    pending_hold_amount: Decimal = ZERO
    pending_charge_amount: Decimal = ZERO

    def recalculate(self) -> None:
        # pending until success/failure of the same kind
        authorized = charged = auth_pending = charge_pending = ZERO
        for e in self.events:
            match e.type:
                case TransactionEventType.HOLD_PLACED:
                    authorized += e.amount
                    auth_pending = ZERO
                case TransactionEventType.HOLD_DECLINED:
                    auth_pending = ZERO
                case TransactionEventType.CHARGE_SETTLED:
                    charged += e.amount
                    charge_pending = ZERO
                case TransactionEventType.CHARGE_DECLINED:
                    charge_pending = ZERO
                case TransactionEventType.HOLD_REQUESTED | TransactionEventType.HOLD_NEEDS_ACTION:
                    auth_pending = e.amount
                case TransactionEventType.CHARGE_REQUESTED | TransactionEventType.CHARGE_NEEDS_ACTION:
                    charge_pending = e.amount

        self.authorized_amount = authorized
        self.charged_amount = charged
        self.pending_hold_amount = auth_pending
        self.pending_charge_amount = charge_pending


class LegacyChargeStatus(StrEnum):
    NOT_CHARGED = auto()
    PENDING = auto()
    AUTHORIZED = auto()
    CHARGED = auto()
    REFUSED = auto()
    CANCELLED = auto()


@dataclass(eq=False, kw_only=True)
class LegacyPayment:
    gateway: str
    currency: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    checkout_id: uuid.UUID | None = None
    order_id: uuid.UUID | None = None
    amount: Decimal = ZERO
    authorized_amount: Decimal = ZERO
    captured_amount: Decimal = ZERO
    status: LegacyChargeStatus = LegacyChargeStatus.NOT_CHARGED
    provider_reference: str | None = None
    created_at: datetime
    is_active: bool = True

    @property
    def covered(self) -> Decimal:
        return self.authorized_amount + self.captured_amount


class ChargeStatus(StrEnum):
    NONE = auto()
    PARTIAL = auto()
    FULL = auto()
    OVERCHARGED = auto()


class AuthorizeStatus(StrEnum):
    NONE = auto()
    PARTIAL = auto()
    FULL = auto()
