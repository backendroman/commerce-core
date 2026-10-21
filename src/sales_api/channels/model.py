from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum, auto

from ..taxes.model import TaxConfiguration


class AllocationStrategy(StrEnum):
    PRIORITIZE_SORTING_ORDER = auto()
    PRIORITIZE_HIGH_STOCK = auto()


class PaymentAction(StrEnum):
    HOLD = auto()
    CHARGE = auto()


@dataclass(eq=False, kw_only=True)
class Channel:
    name: str
    slug: str
    currency: str
    default_country: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    is_active: bool = True

    warehouse_ids: list[uuid.UUID] = field(default_factory=list)
    shipping_zone_ids: set[uuid.UUID] = field(default_factory=set)

    allocation_strategy: AllocationStrategy = AllocationStrategy.PRIORITIZE_SORTING_ORDER

    automatically_confirm_orders: bool = True
    allow_unpaid_orders: bool = False

    automatically_complete_fully_paid_checkouts: bool = False
    automatic_completion_delay: timedelta = timedelta(0)

    default_payment_action: PaymentAction = PaymentAction.CHARGE

    taxes: TaxConfiguration = field(default_factory=TaxConfiguration)

    metadata: dict[str, str] = field(default_factory=dict)
    private_metadata: dict[str, str] = field(default_factory=dict)
