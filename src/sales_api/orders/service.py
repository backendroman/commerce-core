from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from ..common.errors import DomainException, ErrorCodes, ensure, must_exist
from ..common.metadata import matches_filter, patch
from ..common.pagination import Page, SortDirection, paginate
from ..events import payloads
from ..events.domain_event import EventTypes
from ..payments.model import ChargeStatus
from ..permissions.model import Permission, Principal
from ..persistence.store import Store
from ..shop_context import ShopContext, require_permission, require_user
from .model import Order
from .view import OrderView


@dataclass(frozen=True, kw_only=True)
class OrderFilter:
    channel_id: uuid.UUID | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    charge_status: ChargeStatus | None = None
    customer: str | None = None
    number: int | None = None
    metadata: tuple[str, str | None] | None = None


def _transactions_of(store: Store, order: Order):
    return [t for t in store.transactions.values() if t.order_id == order.id]


class OrderService:
    def __init__(self, ctx: ShopContext):
        self._ctx = ctx

    def get_by_guest_token(self, token: uuid.UUID) -> OrderView:
        def get(store: Store) -> OrderView:
            order = must_exist(store.orders.find_by_checkout_token(token), "token", token)
            return OrderView.masked(order)

        return self._ctx.db.read(get)

    def my_orders(self, principal: Principal) -> list[OrderView]:
        require_user(principal)

        def query(store: Store) -> list[OrderView]:
            orders = sorted(
                (o for o in store.orders.values() if o.user_id == principal.user_id),
                key=lambda o: o.number,
                reverse=True,
            )
            return [OrderView.full(o, _transactions_of(store, o)) for o in orders]

        return self._ctx.db.read(query)

    def list(
        self, principal: Principal, criteria: OrderFilter | None = None, first: int = 20, after: str | None = None
    ) -> Page[OrderView]:
        require_permission(principal, Permission.MANAGE_ORDERS)
        f = criteria or OrderFilter()

        def query(store: Store) -> Page[OrderView]:
            def matches(o: Order) -> bool:
                if not principal.can_see_orders_of(o.channel_id):
                    return False
                if f.channel_id is not None and o.channel_id != f.channel_id:
                    return False
                if f.created_from is not None and not o.created_at >= f.created_from:
                    return False
                if f.created_to is not None and not o.created_at <= f.created_to:
                    return False
                if f.charge_status is not None and o.charge_status != f.charge_status:
                    return False
                if f.number is not None and o.number != f.number:
                    return False
                if f.customer is not None:
                    user = store.users.get(o.user_id) if o.user_id is not None else None
                    needle = f.customer.casefold()
                    if not (needle in o.email.casefold() or (user is not None and needle in user.full_name.casefold())):
                        return False
                return not (f.metadata is not None and not matches_filter(o.metadata, f.metadata[0], f.metadata[1]))

            orders = [o for o in store.orders.values() if matches(o)]
            page = paginate(orders, lambda o: [o.number, o.id], first, after, SortDirection.DESC)
            return Page(
                [OrderView.full(o, _transactions_of(store, o)) for o in page.items],
                page.end_cursor,
                page.has_next_page,
                page.total_count,
            )

        return self._ctx.db.read(query)

    def get(self, principal: Principal, order_id: uuid.UUID) -> OrderView:
        def get(store: Store) -> OrderView:
            order = must_exist(store.orders.get(order_id), "id", order_id)
            own = principal.user_id is not None and order.user_id == principal.user_id
            if not own and not principal.can_see_orders_of(order.channel_id):
                raise DomainException(ErrorCodes.FORBIDDEN, "id", "You can't access this order.")
            return OrderView.full(order, _transactions_of(store, order))

        return self._ctx.db.read(get)

    def update_metadata(
        self,
        principal: Principal,
        order_id: uuid.UUID,
        upsert: Mapping[str, str] | None,
        remove: Sequence[str] | None = None,
        is_private: bool = False,
    ) -> None:
        ctx = self._ctx

        def update(store: Store) -> None:
            order = must_exist(store.orders.get(order_id), "id", order_id)
            ensure(
                principal.can_see_orders_of(order.channel_id),
                ErrorCodes.FORBIDDEN,
                None,
                "Requires manage_orders for this channel.",
            )
            patch(order.private_metadata if is_private else order.metadata, upsert, remove)
            ctx.raise_event(
                EventTypes.METADATA_UPDATED, payloads.ref(order.id, f"order #{order.number}"), order.channel_id
            )

        ctx.db.write(update)
