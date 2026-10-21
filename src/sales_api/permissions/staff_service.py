from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from ..accounts.model import User
from ..accounts.security import PasswordHasher, TokenTypes
from ..accounts.service import AccountService, _link, delete_user_data
from ..common.errors import DomainException, ErrorCodes, ensure, must_exist
from ..events import payloads
from ..events.domain_event import EventTypes
from ..notifications import NotificationType, Notifier
from ..persistence.store import Store
from ..shop_context import ShopContext, require_permission
from . import resolver
from .model import Permission, PermissionGroup, Principal, PrincipalKind


@dataclass(frozen=True, kw_only=True)
class GroupInput:
    name: str
    permissions: tuple[Permission, ...] = ()
    user_ids: tuple[uuid.UUID, ...] = ()
    restricted_to_channels: bool = False
    channel_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True, kw_only=True)
class GroupUpdateInput:
    name: str | None = None
    add_permissions: tuple[Permission, ...] = ()
    remove_permissions: tuple[Permission, ...] = ()
    add_users: tuple[uuid.UUID, ...] = ()
    remove_users: tuple[uuid.UUID, ...] = ()
    restricted_to_channels: bool | None = None
    channel_ids: tuple[uuid.UUID, ...] | None = None


class StaffService:
    def __init__(self, ctx: ShopContext, accounts: AccountService, notifier: Notifier):
        self._ctx = ctx
        self._accounts = accounts
        self._notifier = notifier

    @staticmethod
    def create_owner(ctx: ShopContext, email: str, password: str) -> User:
        def create(store: Store) -> User:
            user = store.find_user_by_email(email) or User(email=email.strip(), created_at=ctx.now)
            user.password_hash = PasswordHasher.hash(password)
            user.is_staff = True
            user.is_active = True
            user.is_confirmed = True
            user.is_owner = True
            store.users.add_if_missing(user.id, user)
            return user

        return ctx.db.write(create)

    @staticmethod
    def revoke_owner(ctx: ShopContext, user_id: uuid.UUID) -> None:
        def revoke(store: Store) -> None:
            store.users[user_id].is_owner = False

        ctx.db.write(revoke)

    def invite_staff(self, actor: Principal, email: str, group_ids: Sequence[uuid.UUID], redirect_url: str) -> User:
        ctx = self._ctx
        require_permission(actor, Permission.MANAGE_STAFF)
        self._accounts.validate_redirect(redirect_url)

        def invite(store: Store) -> User:
            ensure(
                store.find_user_by_email(email) is None,
                ErrorCodes.DUPLICATE,
                "email",
                "A user with this email already exists.",
            )
            groups = [must_exist(store.permission_groups.get(id_), "addGroups", id_) for id_ in group_ids]
            _ensure_within_scope(actor, (p for g in groups for p in g.permissions))

            staff = User(email=email.strip(), is_staff=True, is_active=True, is_confirmed=True, created_at=ctx.now)
            store.users.add(staff.id, staff)
            for g in groups:
                g.user_ids.add(staff.id)
            ctx.raise_event(EventTypes.STAFF_CREATED, payloads.user(staff))
            return staff

        user = ctx.db.write(invite)
        token = self._accounts.tokens.issue(
            TokenTypes.PASSWORD_RESET,
            user.id,
            ctx.now + ctx.options.email_confirmation_ttl,
            {"stamp": user.password_stamp.hex},
        )
        self._notifier.send(
            NotificationType.STAFF_INVITE,
            user.email,
            None,
            {"link": _link(redirect_url, user.email, token), "token": token},
        )
        return user

    def set_staff_active(self, actor: Principal, user_id: uuid.UUID, is_active: bool) -> None:
        ctx = self._ctx
        require_permission(actor, Permission.MANAGE_STAFF)

        def update(store: Store) -> None:
            target = must_exist(store.users.get(user_id), "id", user_id)
            _ensure_can_modify_staff(store, actor, target, self_forbidden=not is_active)

            def change(s: Store) -> None:
                s.users[user_id].is_active = is_active

            _ensure_no_permission_orphaned(store, change)
            target.is_active = is_active
            ctx.raise_event(EventTypes.STAFF_UPDATED, payloads.user(target))

        ctx.db.write(update)

    def delete_staff(self, actor: Principal, user_id: uuid.UUID) -> None:
        ctx = self._ctx
        require_permission(actor, Permission.MANAGE_STAFF)

        def delete(store: Store) -> None:
            target = must_exist(store.users.get(user_id), "id", user_id)
            ensure(target.is_staff, ErrorCodes.INVALID, "id", "Not a staff member.")
            _ensure_can_modify_staff(store, actor, target, self_forbidden=True)
            _ensure_no_permission_orphaned(store, lambda s: s.users.remove(user_id))
            delete_user_data(store, target)
            ctx.raise_event(EventTypes.STAFF_DELETED, payloads.user(target))

        ctx.db.write(delete)

    def set_order_notification_subscription(self, actor: Principal, subscribed: bool) -> None:
        if actor.kind != PrincipalKind.STAFF or actor.user_id is None:
            raise DomainException(ErrorCodes.FORBIDDEN, None, "Only staff can subscribe.")
        user_id = actor.user_id

        def update(store: Store) -> None:
            store.users[user_id].subscribed_to_order_notifications = subscribed

        self._ctx.db.write(update)

    def create_group(self, actor: Principal, data: GroupInput) -> PermissionGroup:
        ctx = self._ctx
        require_permission(actor, Permission.MANAGE_STAFF)

        def create(store: Store) -> PermissionGroup:
            _ensure_within_scope(actor, data.permissions)
            for uid in data.user_ids:
                _ensure_can_modify_staff(
                    store, actor, must_exist(store.users.get(uid), "addUsers", uid), self_forbidden=False
                )
            for cid in data.channel_ids:
                must_exist(store.channels.get(cid), "addChannels", cid)

            group = PermissionGroup(name=data.name, restricted_to_channels=data.restricted_to_channels)
            group.permissions.update(data.permissions)
            group.user_ids.update(data.user_ids)
            group.channel_ids.update(data.channel_ids)
            store.permission_groups[group.id] = group
            ctx.raise_event(EventTypes.PERMISSION_GROUP_UPDATED, payloads.ref(group.id, group.name))
            return group

        return ctx.db.write(create)

    def update_group(self, actor: Principal, group_id: uuid.UUID, data: GroupUpdateInput) -> PermissionGroup:
        ctx = self._ctx
        require_permission(actor, Permission.MANAGE_STAFF)

        def update(store: Store) -> PermissionGroup:
            group = must_exist(store.permission_groups.get(group_id), "id", group_id)
            # Staff can only manage groups/permissions that are a subset of their own.
            _ensure_within_scope(actor, group.permissions)
            _ensure_within_scope(actor, data.add_permissions)
            for uid in (*data.add_users, *data.remove_users):
                user = must_exist(store.users.get(uid), "users", uid)
                _ensure_can_modify_staff(store, actor, user, self_forbidden=False)

            def apply(g: PermissionGroup) -> None:
                g.permissions.update(data.add_permissions)
                g.permissions.difference_update(data.remove_permissions)
                g.user_ids.update(data.add_users)
                g.user_ids.difference_update(data.remove_users)

            _ensure_no_permission_orphaned(store, lambda s: apply(s.permission_groups[group_id]))
            apply(group)
            if data.name is not None:
                group.name = data.name
            if data.restricted_to_channels is not None:
                group.restricted_to_channels = data.restricted_to_channels
            if data.channel_ids is not None:
                group.channel_ids.clear()
                group.channel_ids.update(data.channel_ids)

            ctx.raise_event(EventTypes.PERMISSION_GROUP_UPDATED, payloads.ref(group.id, group.name))
            return group

        return ctx.db.write(update)

    def delete_group(self, actor: Principal, group_id: uuid.UUID) -> None:
        ctx = self._ctx
        require_permission(actor, Permission.MANAGE_STAFF)

        def delete(store: Store) -> None:
            group = must_exist(store.permission_groups.get(group_id), "id", group_id)
            _ensure_within_scope(actor, group.permissions)
            _ensure_no_permission_orphaned(store, lambda s: s.permission_groups.pop(group_id, None))
            del store.permission_groups[group_id]
            ctx.raise_event(EventTypes.PERMISSION_GROUP_UPDATED, payloads.ref(group.id, group.name))

        ctx.db.write(delete)


def _ensure_within_scope(actor: Principal, permissions: Iterable[Permission]) -> None:
    missing = list(dict.fromkeys(p for p in permissions if not actor.has(p)))
    if missing:
        raise DomainException(
            ErrorCodes.BEYOND_OWN_PERMISSIONS,
            "permissions",
            f"You can't manage permissions you don't have: {', '.join(str(p) for p in missing)}.",
        )


def _ensure_can_modify_staff(store: Store, actor: Principal, target: User, self_forbidden: bool) -> None:
    if self_forbidden and actor.user_id == target.id:
        raise DomainException(ErrorCodes.SELF_CHANGE, "id", "You can't deactivate or delete yourself.")
    if target.is_owner and not actor.is_owner:
        raise DomainException(ErrorCodes.OWNER_ONLY, "id", "Only an owner can modify an owner.")
    _ensure_within_scope(actor, resolver.user_permissions(store, target))


def _ensure_no_permission_orphaned(store: Store, change: Callable[[Store], object]) -> None:
    before = _manageable(store)
    copy = _snapshot(store)
    change(copy)
    lost = sorted(before - _manageable(copy))
    if lost:
        raise DomainException(
            ErrorCodes.PERMISSION_ORPHANED,
            "permissions",
            f"No active staff member with manage_staff would hold: {', '.join(str(p) for p in lost)}.",
        )


def _manageable(store: Store) -> set[Permission]:
    result: set[Permission] = set()
    for user in store.users.values():
        if not (user.is_staff and user.is_active):
            continue
        permissions = resolver.user_permissions(store, user)
        if Permission.MANAGE_STAFF in permissions:
            result.update(permissions)
    return result


def _snapshot(store: Store) -> Store:
    copy = Store()
    for u in store.users.values():
        clone = dataclasses.replace(u)
        copy.users.add(clone.id, clone)

    for g in store.permission_groups.values():
        clone_group = PermissionGroup(id=g.id, name=g.name)
        clone_group.permissions.update(g.permissions)
        clone_group.user_ids.update(g.user_ids)
        copy.permission_groups[clone_group.id] = clone_group

    return copy
