from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum, auto

from ..common.address import Address


class ClickAndCollectOption(StrEnum):
    DISABLED = auto()
    LOCAL = auto()
    ALL_WAREHOUSES = auto()


@dataclass(eq=False, kw_only=True)
class Warehouse:
    name: str
    slug: str
    address: Address
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    email: str = ""
    click_and_collect: ClickAndCollectOption = ClickAndCollectOption.DISABLED
    created_at: datetime
    metadata: dict[str, str] = field(default_factory=dict)
    private_metadata: dict[str, str] = field(default_factory=dict)


@dataclass(eq=False, kw_only=True)
class Stock:
    warehouse_id: uuid.UUID
    variant_id: uuid.UUID
    quantity_in_stock: int = 0
    quantity_allocated: int = 0
    low_stock_threshold: int | None = None


@dataclass(eq=False, kw_only=True)
class Allocation:
    order_id: uuid.UUID
    order_line_id: uuid.UUID
    warehouse_id: uuid.UUID
    variant_id: uuid.UUID
    quantity: int = 0
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass(eq=False, kw_only=True)
class PreorderAllocation:
    order_id: uuid.UUID
    order_line_id: uuid.UUID
    channel_id: uuid.UUID
    variant_id: uuid.UUID
    quantity: int = 0
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass(eq=False, kw_only=True)
class Reservation:
    checkout_id: uuid.UUID
    checkout_line_id: uuid.UUID
    variant_id: uuid.UUID
    reserved_until: datetime
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    warehouse_id: uuid.UUID | None = None
    preorder_channel_id: uuid.UUID | None = None
    quantity: int = 0

    def is_active_at(self, now: datetime) -> bool:
        return self.reserved_until > now
