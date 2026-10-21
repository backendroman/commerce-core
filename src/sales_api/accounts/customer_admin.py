from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from ..common.address import Address
from ..common.errors import ErrorCodes, ensure, must_exist
from ..common.metadata import matches_filter, patch
from ..common.pagination import Page, paginate
from ..events import payloads
from ..events.domain_event import EventTypes
from ..permissions.model import Permission, Principal
from ..persistence.store import Store
from ..shop_context import ShopContext, require_permission
from .model import CustomerEvent, CustomerNote, User, UserAddress
from .service import AccountService, delete_user_data


@dataclass(frozen=True, kw_only=True)
class CustomerInput:
    email: str
    first_name: str = ""
    last_name: str = ""
    is_active: bool = True
    note: str | None = None
    addresses: tuple[Address, ...] = ()


@dataclass(frozen=True, kw_only=True)
class CustomerFilter:
    search: str | None = None
    joined_from: datetime | None = None
    joined_to: datetime | None = None
    min_orders: int | None = None
    max_orders: int | None = None
    last_order_from: datetime | None = None
    metadata: tuple[str, str | None] | None = None


class CustomerAdminService:
    def __init__(self, ctx: ShopContext, accounts: AccountService):
        self._ctx = ctx
        self._accounts = accounts

    def create(self, principal: Principal, data: CustomerInput) -> User:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_USERS)
        addresses = [ctx.addresses.normalize(a) for a in data.addresses]

        def create(store: Store) -> User:
            ensure(
                store.find_user_by_email(data.email) is None,
                ErrorCodes.DUPLICATE,
                "email",
                "A user with this email already exists.",
            )
            user = User(
                email=data.email.strip(),
                first_name=data.first_name,
                last_name=data.last_name,
                is_active=data.is_active,
                created_at=ctx.now,
            )
            for a in addresses:
                user.addresses.append(UserAddress(uuid.uuid4(), a))
            user.default_shipping_address_id = user.addresses[0].id if user.addresses else None
            user.default_billing_address_id = user.addresses[0].id if user.addresses else None
            if data.note is not None:
                user.notes.append(CustomerNote(ctx.now, principal.user_id, data.note))
            user.events.append(CustomerEvent(ctx.now, "CREATED_BY_STAFF", principal.user_id))
            store.users.add(user.id, user)
            ctx.raise_event(EventTypes.CUSTOMER_CREATED, payloads.user(user))
            return user

        return ctx.db.write(create)

    def set_active(self, principal: Principal, user_id: uuid.UUID, active: bool) -> None:
        def change(u: User) -> None:
            u.is_active = active
            u.events.append(CustomerEvent(self._ctx.now, "ACTIVATED" if active else "DEACTIVATED", principal.user_id))

        self._update(principal, user_id, change)

    def add_note(self, principal: Principal, user_id: uuid.UUID, text: str) -> None:
        def change(u: User) -> None:
            u.notes.append(CustomerNote(self._ctx.now, principal.user_id, text))
            u.events.append(CustomerEvent(self._ctx.now, "NOTE", principal.user_id))

        self._update(principal, user_id, change)

    def update_metadata(
        self,
        principal: Principal,
        user_id: uuid.UUID,
        upsert: Mapping[str, str] | None,
        remove: Sequence[str] | None = None,
        is_private: bool = False,
    ) -> None:
        self._update(
            principal, user_id, lambda u: patch(u.private_metadata if is_private else u.metadata, upsert, remove)
        )

    def delete(self, principal: Principal, user_id: uuid.UUID) -> None:
        self.delete_many(principal, [user_id])

    def delete_many(self, principal: Principal, user_ids: Sequence[uuid.UUID]) -> None:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_USERS)

        def delete(store: Store) -> None:
            users = [must_exist(store.users.get(id_), "ids", id_) for id_ in user_ids]
            ensure(
                all(not u.is_staff for u in users),
                ErrorCodes.INVALID,
                "ids",
                "Use staff management to delete staff members.",
            )
            for user in users:
                delete_user_data(store, user)
                ctx.raise_event(EventTypes.CUSTOMER_DELETED, payloads.user(user))

        ctx.db.write(delete)

    def send_set_password_link(self, principal: Principal, user_id: uuid.UUID, redirect_url: str) -> None:
        require_permission(principal, Permission.MANAGE_USERS)
        email = self._ctx.db.read(lambda store: must_exist(store.users.get(user_id), "id", user_id).email)
        self._accounts.request_password_reset(email, redirect_url)

    def list(
        self, principal: Principal, criteria: CustomerFilter | None = None, first: int = 20, after: str | None = None
    ) -> Page[User]:
        require_permission(principal, Permission.MANAGE_USERS)
        f = criteria or CustomerFilter()

        def query(store: Store) -> Page[User]:
            order_stats: dict[uuid.UUID, tuple[int, datetime]] = {}
            for o in store.orders.values():
                if o.user_id is None:
                    continue
                count, last = order_stats.get(o.user_id, (0, o.created_at))
                order_stats[o.user_id] = (count + 1, max(last, o.created_at))

            def matches(u: User) -> bool:
                if u.is_staff:
                    return False
                if f.search is not None and not (
                    f.search.casefold() in u.email.casefold()
                    or f.search.casefold() in u.full_name.casefold()
                    or any(f.search in a.address.phone for a in u.addresses)
                ):
                    return False
                if f.joined_from is not None and not u.created_at >= f.joined_from:
                    return False
                if f.joined_to is not None and not u.created_at <= f.joined_to:
                    return False
                count = order_stats.get(u.id, (0, None))[0]
                if f.min_orders is not None and not count >= f.min_orders:
                    return False
                if f.max_orders is not None and not count <= f.max_orders:
                    return False
                if f.last_order_from is not None and not (
                    u.id in order_stats and order_stats[u.id][1] >= f.last_order_from
                ):
                    return False
                return not (f.metadata is not None and not matches_filter(u.metadata, f.metadata[0], f.metadata[1]))

            users = [u for u in store.users.values() if matches(u)]
            return paginate(users, lambda u: [u.email.casefold(), u.id], first, after)

        return self._ctx.db.read(query)

    def _update(self, principal: Principal, user_id: uuid.UUID, change: Callable[[User], None]) -> None:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_USERS)

        def update(store: Store) -> None:
            user = must_exist(store.users.get(user_id), "id", user_id)
            ensure(not user.is_staff, ErrorCodes.INVALID, "id", "Use staff management for staff members.")
            change(user)
            ctx.raise_event(EventTypes.CUSTOMER_UPDATED, payloads.user(user))

        ctx.db.write(update)
