from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum, StrEnum, auto


class Permission(StrEnum):
    MANAGE_PRODUCTS = auto()
    MANAGE_PRODUCT_TYPES_AND_ATTRIBUTES = auto()
    MANAGE_ORDERS = auto()
    MANAGE_USERS = auto()
    MANAGE_STAFF = auto()
    MANAGE_DISCOUNTS = auto()
    MANAGE_GIFT_CARDS = auto()
    MANAGE_CHANNELS = auto()
    MANAGE_SHIPPING = auto()
    MANAGE_TAXES = auto()
    MANAGE_PAGES = auto()
    MANAGE_PAGE_TYPES_AND_ATTRIBUTES = auto()
    MANAGE_MENUS = auto()
    MANAGE_TRANSLATIONS = auto()
    MANAGE_SETTINGS = auto()
    MANAGE_APPS = auto()
    MANAGE_PLUGINS = auto()
    HANDLE_CHECKOUTS = auto()
    HANDLE_PAYMENTS = auto()


ALL_PERMISSIONS: frozenset[Permission] = frozenset(Permission)


class PrincipalKind(Enum):
    ANONYMOUS = "anonymous"
    CUSTOMER = "customer"
    STAFF = "staff"
    APP = "app"


@dataclass(frozen=True, kw_only=True)
class Principal:
    kind: PrincipalKind = PrincipalKind.ANONYMOUS
    user_id: uuid.UUID | None = None
    app_id: uuid.UUID | None = None
    is_owner: bool = False
    permissions: frozenset[Permission] = frozenset()
    order_channels: frozenset[uuid.UUID] | None = None

    @property
    def is_staff_or_app(self) -> bool:
        return self.kind in (PrincipalKind.STAFF, PrincipalKind.APP)

    def has(self, permission: Permission) -> bool:
        return self.is_owner or permission in self.permissions

    def can_see_orders_of(self, channel_id: uuid.UUID) -> bool:
        return self.has(Permission.MANAGE_ORDERS) and (self.order_channels is None or channel_id in self.order_channels)


ANONYMOUS = Principal(kind=PrincipalKind.ANONYMOUS)


@dataclass(eq=False, kw_only=True)
class PermissionGroup:
    name: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    permissions: set[Permission] = field(default_factory=set)
    user_ids: set[uuid.UUID] = field(default_factory=set)
    restricted_to_channels: bool = False
    channel_ids: set[uuid.UUID] = field(default_factory=set)
