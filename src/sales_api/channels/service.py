from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta

from ..common.errors import DomainException, ErrorCodes, ErrorCollector, ensure, must_exist
from ..common.money import is_currency_code
from ..common.slug import unique_slug
from ..events import payloads
from ..events.domain_event import EventTypes
from ..permissions.model import Permission, Principal
from ..persistence.store import Store
from ..shop_context import ShopContext, require_permission
from .model import AllocationStrategy, Channel, PaymentAction


@dataclass(frozen=True, kw_only=True)
class ChannelCreateInput:
    name: str
    currency: str
    default_country: str
    slug: str | None = None
    is_active: bool = True
    allocation_strategy: AllocationStrategy = AllocationStrategy.PRIORITIZE_SORTING_ORDER
    automatically_confirm_orders: bool = True
    allow_unpaid_orders: bool = False
    default_payment_action: PaymentAction = PaymentAction.CHARGE
    warehouse_ids: tuple[uuid.UUID, ...] = ()
    shipping_zone_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ChannelUpdateInput:
    name: str | None = None
    slug: str | None = None
    default_country: str | None = None
    is_active: bool | None = None
    allocation_strategy: AllocationStrategy | None = None
    automatically_confirm_orders: bool | None = None
    allow_unpaid_orders: bool | None = None
    automatically_complete_fully_paid_checkouts: bool | None = None
    automatic_completion_delay: timedelta | None = None
    default_payment_action: PaymentAction | None = None
    warehouse_ids: tuple[uuid.UUID, ...] | None = None
    shipping_zone_ids: tuple[uuid.UUID, ...] | None = None


class ChannelService:
    def __init__(self, ctx: ShopContext):
        self._ctx = ctx

    def create(self, principal: Principal, data: ChannelCreateInput) -> Channel:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_CHANNELS)

        def create(store: Store) -> Channel:
            errors = ErrorCollector()
            if not data.name or not data.name.strip():
                errors.add(ErrorCodes.REQUIRED, "name", "Name is required.")
            if not is_currency_code(data.currency):
                errors.add(ErrorCodes.INVALID, "currencyCode", "Currency must be an ISO 4217 code.")
            if data.default_country is None or len(data.default_country) != 2:
                errors.add(ErrorCodes.INVALID, "defaultCountry", "Country must be an ISO 3166-1 alpha-2 code.")
            _validate_refs(store, data.warehouse_ids, data.shipping_zone_ids, errors)
            errors.raise_if_any()

            channel = Channel(
                name=data.name.strip(),
                slug=unique_slug(data.slug, data.name, lambda s: any(c.slug == s for c in store.channels.values())),
                currency=data.currency,
                default_country=data.default_country.upper(),
                is_active=data.is_active,
                allocation_strategy=data.allocation_strategy,
                automatically_confirm_orders=data.automatically_confirm_orders,
                allow_unpaid_orders=data.allow_unpaid_orders,
                default_payment_action=data.default_payment_action,
            )
            channel.warehouse_ids.extend(dict.fromkeys(data.warehouse_ids))
            channel.shipping_zone_ids.update(data.shipping_zone_ids)
            store.channels[channel.id] = channel
            ctx.raise_event(EventTypes.CHANNEL_CREATED, payloads.channel(channel), channel.id)
            return channel

        return ctx.db.write(create)

    def update(self, principal: Principal, channel_id: uuid.UUID, data: ChannelUpdateInput) -> Channel:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_CHANNELS)

        def update(store: Store) -> Channel:
            channel = must_exist(store.channels.get(channel_id), "channel", channel_id)
            errors = ErrorCollector()
            if data.default_country is not None and len(data.default_country) != 2:
                errors.add(ErrorCodes.INVALID, "defaultCountry", "Invalid country code.")
            _validate_refs(store, data.warehouse_ids or (), data.shipping_zone_ids or (), errors)
            errors.raise_if_any()

            if data.slug is not None and data.slug != channel.slug:
                channel.slug = unique_slug(
                    data.slug,
                    channel.name,
                    lambda s: any(c.slug == s and c.id != channel_id for c in store.channels.values()),
                )
            if data.name is not None:
                channel.name = data.name.strip()
            if data.default_country is not None:
                channel.default_country = data.default_country.upper()
            if data.is_active is not None:
                channel.is_active = data.is_active
            if data.allocation_strategy is not None:
                channel.allocation_strategy = data.allocation_strategy
            if data.automatically_confirm_orders is not None:
                channel.automatically_confirm_orders = data.automatically_confirm_orders
            if data.allow_unpaid_orders is not None:
                channel.allow_unpaid_orders = data.allow_unpaid_orders
            if data.automatically_complete_fully_paid_checkouts is not None:
                channel.automatically_complete_fully_paid_checkouts = data.automatically_complete_fully_paid_checkouts
            if data.automatic_completion_delay is not None:
                channel.automatic_completion_delay = data.automatic_completion_delay
            if data.default_payment_action is not None:
                channel.default_payment_action = data.default_payment_action
            if data.warehouse_ids is not None:
                channel.warehouse_ids.clear()
                channel.warehouse_ids.extend(dict.fromkeys(data.warehouse_ids))
            if data.shipping_zone_ids is not None:
                channel.shipping_zone_ids.clear()
                channel.shipping_zone_ids.update(data.shipping_zone_ids)

            ctx.raise_event(EventTypes.CHANNEL_UPDATED, payloads.channel(channel), channel.id)
            return channel

        return ctx.db.write(update)

    def delete(self, principal: Principal, channel_id: uuid.UUID, target_channel_id: uuid.UUID | None = None) -> None:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_CHANNELS)

        def delete(store: Store) -> None:
            channel = must_exist(store.channels.get(channel_id), "channel", channel_id)
            checkouts = [c for c in store.checkouts.values() if c.channel_id == channel_id]
            orders = [o for o in store.orders.values() if o.channel_id == channel_id]

            if checkouts or orders:
                if target_channel_id is None:
                    raise DomainException(
                        ErrorCodes.INVALID,
                        "channelId",
                        "Channel has checkouts or orders; a target channel is required.",
                    )
                target = must_exist(store.channels.get(target_channel_id), "targetChannel", target_channel_id)
                ensure(
                    target.id != channel.id,
                    ErrorCodes.INVALID,
                    "targetChannel",
                    "Target must differ from the deleted channel.",
                )
                ensure(
                    target.currency == channel.currency,
                    ErrorCodes.CURRENCY_MISMATCH,
                    "targetChannel",
                    "Target channel must use the same currency.",
                )

                for checkout in checkouts:
                    checkout.channel_id = target.id
                for order in orders:
                    order.channel_id = target.id

            for product in store.products.values():
                product.channel_listings.pop(channel_id, None)
            for variant in store.variants.values():
                variant.channel_listings.pop(channel_id, None)
            for collection in store.collections.values():
                collection.channel_listings.pop(channel_id, None)
            for method in store.shipping_methods:
                method.channel_listings.pop(channel_id, None)
            for group in store.permission_groups.values():
                group.channel_ids.discard(channel_id)
            del store.channels[channel_id]
            ctx.raise_event(EventTypes.CHANNEL_DELETED, payloads.channel(channel), channel_id)

        ctx.db.write(delete)


def _validate_refs(
    store: Store, warehouse_ids: Iterable[uuid.UUID], zone_ids: Iterable[uuid.UUID], errors: ErrorCollector
) -> None:
    for id_ in warehouse_ids:
        if id_ not in store.warehouses:
            errors.add(ErrorCodes.NOT_FOUND, "warehouseIds", f"Warehouse {id_} not found.")
    for id_ in zone_ids:
        if id_ not in store.shipping_zones:
            errors.add(ErrorCodes.NOT_FOUND, "shippingZoneIds", f"Shipping zone {id_} not found.")
