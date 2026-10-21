from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from .model import ALL_PERMISSIONS, Permission, Principal, PrincipalKind

if TYPE_CHECKING:
    from ..accounts.model import User
    from ..apps.model import App
    from ..persistence.store import Store


def user_permissions(store: Store, user: User) -> frozenset[Permission]:
    if not user.is_staff or not user.is_active:
        return frozenset()
    if user.is_owner:
        return ALL_PERMISSIONS
    return frozenset(p for g in store.permission_groups.values() if user.id in g.user_ids for p in g.permissions)


def order_channels(store: Store, user: User) -> frozenset[uuid.UUID] | None:
    if user.is_owner:
        return None
    groups = [
        g
        for g in store.permission_groups.values()
        if user.id in g.user_ids and Permission.MANAGE_ORDERS in g.permissions
    ]
    if any(not g.restricted_to_channels for g in groups):
        return None
    return frozenset(c for g in groups for c in g.channel_ids)


def for_user(store: Store, user: User) -> Principal:
    return Principal(
        kind=PrincipalKind.STAFF if user.is_staff else PrincipalKind.CUSTOMER,
        user_id=user.id,
        is_owner=user.is_owner and user.is_active,
        permissions=user_permissions(store, user),
        order_channels=order_channels(store, user),
    )


def for_app(app: App) -> Principal:
    return Principal(
        kind=PrincipalKind.APP,
        app_id=app.id,
        permissions=frozenset(app.permissions) if app.is_active else frozenset(),
    )
