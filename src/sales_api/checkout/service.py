from __future__ import annotations

import dataclasses
import threading
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from functools import partial
from typing import TYPE_CHECKING, Any

from ..apps.handlers import (
    AppCallOk,
    PaymentAppHandler,
    ShippingAppHandler,
    ShippingAppRequest,
    ShippingFilterAppHandler,
    call_app,
)
from ..channels.model import Channel
from ..common.address import Address
from ..common.errors import DomainError, DomainException, ErrorCodes, ErrorCollector, ensure, must_exist
from ..common.metadata import patch
from ..common.money import ZERO
from ..discounts.rules import OrderRewardCandidate, VoucherContext, validate_voucher
from ..events import payloads
from ..events.domain_event import EventTypes
from ..inventory import stock_logic
from ..inventory.model import ClickAndCollectOption, Warehouse
from ..inventory.stock_logic import LineDemand, StockScope
from ..payments import status as payment_status
from ..payments.status import PaymentSummary
from ..permissions.model import Permission, Principal
from ..persistence.store import Store
from ..shipping.model import ShippingMethod
from ..shipping.service import ShippingContext, ShippingService
from ..shop_context import ShopContext, require_permission, require_user
from . import rules as checkout_rules
from .calculator import CheckoutCalculator, CheckoutPricing
from .model import NO_DELIVERY, Checkout, CheckoutLine, DeliveryKind, DeliverySelection, ExternalShippingMethod
from .rules import CheckoutProblem

if TYPE_CHECKING:
    from ..accounts.model import User


@dataclass(frozen=True, kw_only=True)
class CheckoutLineInput:
    variant_id: uuid.UUID
    quantity: int
    metadata: Mapping[str, str] | None = None
    price_override: Decimal | None = None


@dataclass(frozen=True)
class CheckoutLineUpdate:
    line_id: uuid.UUID
    quantity: int


@dataclass(frozen=True, kw_only=True)
class CheckoutCreateInput:
    channel_id: uuid.UUID
    email: str | None = None
    language_code: str | None = None
    lines: tuple[CheckoutLineInput, ...] = ()
    shipping_address: Address | None = None
    billing_address: Address | None = None


@dataclass(frozen=True, kw_only=True)
class DeliveryInput:
    shipping_method_id: uuid.UUID | None = None
    external_method_id: str | None = None
    collection_point_id: uuid.UUID | None = None


@dataclass(frozen=True)
class AvailablePaymentGateway:
    app_id: uuid.UUID
    id: str
    name: str
    config: Mapping[str, Any]


@dataclass(frozen=True)
class CheckoutView:
    checkout: Checkout
    pricing: CheckoutPricing
    problems: tuple[CheckoutProblem, ...]
    payment: PaymentSummary
    amount_remaining: Decimal


class CheckoutService:
    def __init__(self, ctx: ShopContext, calculator: CheckoutCalculator):
        self._ctx = ctx
        self._calculator = calculator
        self._cache_lock = threading.Lock()
        self._price_cache: dict[uuid.UUID, tuple[int, datetime, CheckoutPricing]] = {}
        self._external_shipping_cache: dict[uuid.UUID, tuple[int, datetime, list[ExternalShippingMethod]]] = {}

    def create(self, principal: Principal, data: CheckoutCreateInput) -> Checkout:
        ctx = self._ctx
        shipping = (
            None if data.shipping_address is None else ctx.addresses.normalize(data.shipping_address, "shippingAddress")
        )
        billing = (
            None if data.billing_address is None else ctx.addresses.normalize(data.billing_address, "billingAddress")
        )

        def create(store: Store) -> Checkout:
            channel = must_exist(store.channels.get(data.channel_id), "channel", data.channel_id)
            ensure(channel.is_active, ErrorCodes.CHANNEL_INACTIVE, "channel", "Channel is inactive.")
            user = store.users.get(principal.user_id) if principal.user_id is not None else None

            checkout = Checkout(
                channel_id=channel.id,
                user_id=user.id if user is not None else None,
                email=data.email.strip() if data.email is not None else (user.email if user is not None else None),
                language_code=data.language_code
                or (user.language_code if user is not None else None)
                or ctx.options.default_language,
                # Use the customer's default addresses
                shipping_address=shipping or _default_address(user, user.default_shipping_address_id if user else None),
                billing_address=billing or _default_address(user, user.default_billing_address_id if user else None),
                created_at=ctx.now,
                last_change_at=ctx.now,
            )

            if data.lines:
                self._apply_line_additions(principal, store, channel, checkout, data.lines)
            store.checkouts.add(checkout.id, checkout)
            self._after_change(store, checkout, channel, refresh_reservations=True)
            ctx.raise_event(EventTypes.CHECKOUT_CREATED, payloads.checkout(checkout), channel.id)
            return checkout

        return ctx.db.write(create)

    def get(self, token: uuid.UUID) -> CheckoutView:
        def get(store: Store) -> CheckoutView:
            checkout, channel = self.load(store, token)
            return self.view(store, checkout, channel)

        return self._ctx.db.write(get)

    def list_checkouts(self, principal: Principal, channel_id: uuid.UUID | None = None) -> list[Checkout]:
        require_permission(principal, Permission.HANDLE_CHECKOUTS)
        return self._ctx.db.read(
            lambda store: [c for c in store.checkouts.values() if channel_id is None or c.channel_id == channel_id]
        )

    def my_checkouts(self, principal: Principal) -> list[Checkout]:
        require_user(principal)
        return self._ctx.db.read(lambda store: [c for c in store.checkouts.values() if c.user_id == principal.user_id])

    def get_pricing(self, token: uuid.UUID, force_recalculate: bool = False) -> CheckoutPricing:
        def get(store: Store) -> CheckoutPricing:
            checkout, _ = self.load(store, token)
            if force_recalculate:
                with self._cache_lock:
                    self._price_cache.pop(checkout.id, None)
            return self.price(store, checkout)

        return self._ctx.db.write(get)

    def forget(self, checkout_id: uuid.UUID) -> None:
        with self._cache_lock:
            self._price_cache.pop(checkout_id, None)
            self._external_shipping_cache.pop(checkout_id, None)

    def price(self, store: Store, checkout: Checkout) -> CheckoutPricing:
        now = self._ctx.now
        with self._cache_lock:
            cached = self._price_cache.get(checkout.id)
        if cached is not None and cached[0] == checkout.version and cached[1] > now:
            return cached[2]
        pricing = self._calculator.calculate(store, checkout)
        with self._cache_lock:
            self._price_cache[checkout.id] = (
                checkout.version,
                now + self._ctx.options.checkout_price_cache_ttl,
                pricing,
            )
        return pricing

    def view(self, store: Store, checkout: Checkout, channel: Channel) -> CheckoutView:
        pricing = self.price(store, checkout)
        payment = payment_status.for_checkout(store, checkout.id, pricing.amount_to_pay)
        return CheckoutView(
            checkout,
            pricing,
            tuple(checkout_rules.problems(store, checkout, channel, self._ctx.now)),
            payment,
            max(ZERO, pricing.amount_to_pay - payment.covered),
        )

    def add_lines(self, principal: Principal, token: uuid.UUID, lines: Sequence[CheckoutLineInput]) -> CheckoutView:
        return self._mutate(
            token,
            True,
            lambda store, checkout, channel: self._apply_line_additions(principal, store, channel, checkout, lines),
        )

    def update_lines(self, token: uuid.UUID, updates: Sequence[CheckoutLineUpdate]) -> CheckoutView:
        def mutation(store: Store, checkout: Checkout, channel: Channel) -> None:
            errors = ErrorCollector()
            lines_by_id = {line.id: line for line in checkout.lines}
            for u in updates:
                line = lines_by_id.get(u.line_id)
                if line is None:
                    errors.add(ErrorCodes.NOT_FOUND, "lineId", f"Line {u.line_id} not found.")
                elif line.is_gift:
                    errors.add(ErrorCodes.INVALID, "lineId", "Gift lines are managed by the promotion.")
                if u.quantity < 0:
                    errors.add(ErrorCodes.INVALID, "quantity", "Quantity cannot be negative.")

            errors.raise_if_any()
            proposed = {line.id: line.quantity for line in checkout.lines}
            for u in updates:
                proposed[u.line_id] = u.quantity
            self._validate_quantities(store, channel, checkout, proposed)

            for u in updates:
                line = lines_by_id[u.line_id]
                if u.quantity == 0:
                    checkout.lines.remove(line)
                else:
                    line.quantity = u.quantity

        return self._mutate(token, True, mutation)

    def delete_lines(self, token: uuid.UUID, line_ids: Sequence[uuid.UUID]) -> CheckoutView:
        return self.update_lines(token, [CheckoutLineUpdate(id_, 0) for id_ in line_ids])

    def _apply_line_additions(
        self,
        principal: Principal,
        store: Store,
        channel: Channel,
        checkout: Checkout,
        inputs: Sequence[CheckoutLineInput],
    ) -> None:
        ctx = self._ctx
        errors = ErrorCollector()
        for input_ in inputs:
            if input_.quantity <= 0:
                errors.add(ErrorCodes.INVALID, "quantity", "Quantity must be greater than zero.")
            if input_.price_override is not None and not principal.has(Permission.HANDLE_CHECKOUTS):
                errors.add(ErrorCodes.FORBIDDEN, "price", "Setting a line price requires handle_checkouts.")
            if input_.price_override is not None and input_.price_override < 0:
                errors.add(ErrorCodes.INVALID, "price", "Price cannot be negative.")
            variant = store.variants.get(input_.variant_id)
            if variant is None:
                errors.add(ErrorCodes.NOT_FOUND, "variantId", f"Variant {input_.variant_id} not found.")
                continue

            reason = checkout_rules.unavailability_reason(store, channel, variant, ctx.now)
            if reason is not None:
                errors.add(DomainError(ErrorCodes.VARIANT_UNAVAILABLE, "variantId", reason, (variant.id,)))

        errors.raise_if_any()

        # Merge into an existing line only if metadata and custom price match.
        proposed = {line.id: line.quantity for line in checkout.lines}
        new_lines: list[CheckoutLine] = []
        for input_ in inputs:
            existing = next(
                (
                    line
                    for line in [*checkout.lines, *new_lines]
                    if line.variant_id == input_.variant_id
                    and not line.is_gift
                    and line.price_override == input_.price_override
                    and _same_metadata(line.metadata, input_.metadata)
                ),
                None,
            )
            if existing is not None:
                proposed[existing.id] = proposed.get(existing.id, 0) + input_.quantity
                continue

            line = CheckoutLine(variant_id=input_.variant_id, price_override=input_.price_override, created_at=ctx.now)
            patch(line.metadata, input_.metadata)
            new_lines.append(line)
            proposed[line.id] = input_.quantity

        self._validate_quantities(store, channel, checkout, proposed, new_lines)
        checkout.lines.extend(new_lines)
        for line in checkout.lines:
            line.quantity = proposed[line.id]

    def _validate_quantities(
        self,
        store: Store,
        channel: Channel,
        checkout: Checkout,
        proposed: Mapping[uuid.UUID, int],
        new_lines: Sequence[CheckoutLine] | None = None,
    ) -> None:
        all_lines = [*checkout.lines, *(new_lines or ())]
        before: dict[uuid.UUID, int] = {}
        for line in checkout.lines:
            before[line.variant_id] = before.get(line.variant_id, 0) + line.quantity
        after: dict[uuid.UUID, int] = {}
        for line in all_lines:
            after[line.variant_id] = after.get(line.variant_id, 0) + proposed.get(line.id, 0)
        max_per_variant = store.settings.max_checkout_line_quantity

        errors = ErrorCollector()
        for variant_id, quantity in after.items():
            variant = store.variants[variant_id]
            if quantity > max_per_variant:
                errors.add(
                    DomainError(
                        ErrorCodes.QUANTITY_LIMIT_EXCEEDED,
                        "quantity",
                        f"Cannot add more than {max_per_variant} units of a variant.",
                        (variant_id,),
                    )
                )
            limit = variant.quantity_limit_per_checkout
            if limit is not None and quantity > limit:
                errors.add(
                    DomainError(
                        ErrorCodes.QUANTITY_LIMIT_EXCEEDED,
                        "quantity",
                        f"Cannot add more than {limit} units of this variant.",
                        (variant_id,),
                    )
                )

        errors.raise_if_any()

        grown = [(v, q) for v, q in after.items() if q > before.get(v, 0)]
        if not grown:
            return
        scope = checkout_rules.stock_scope_of(store, _scope_probe(checkout, all_lines), channel)
        shortages = stock_logic.find_shortages(store, channel, scope, grown, self._ctx.now, checkout.id)
        if shortages:
            raise stock_logic.shortage_error(shortages)

    def set_email(self, token: uuid.UUID, email: str) -> CheckoutView:
        def mutation(_: Store, checkout: Checkout, __: Channel) -> None:
            ensure("@" in email, ErrorCodes.INVALID, "email", "Invalid email.")
            checkout.email = email.strip()

        return self._mutate(token, False, mutation)

    def set_language(self, token: uuid.UUID, language_code: str) -> CheckoutView:
        def mutation(_: Store, checkout: Checkout, __: Channel) -> None:
            checkout.language_code = language_code

        return self._mutate(token, False, mutation)

    def set_customer_note(self, token: uuid.UUID, note: str | None) -> CheckoutView:
        def mutation(_: Store, checkout: Checkout, __: Channel) -> None:
            checkout.customer_note = note

        return self._mutate(token, False, mutation)

    def set_shipping_address(self, token: uuid.UUID, address: Address) -> CheckoutView:
        normalized = self._ctx.addresses.normalize(address, "shippingAddress")

        def mutation(store: Store, checkout: Checkout, channel: Channel) -> None:
            self._check_stock_for_new_address(store, channel, checkout, normalized, checkout.billing_address)
            checkout.shipping_address = normalized

        return self._mutate(token, False, mutation, re_reserve=True)

    def set_billing_address(self, token: uuid.UUID, address: Address) -> CheckoutView:
        normalized = self._ctx.addresses.normalize(address, "billingAddress")

        def mutation(store: Store, checkout: Checkout, channel: Channel) -> None:
            self._check_stock_for_new_address(store, channel, checkout, checkout.shipping_address, normalized)
            checkout.billing_address = normalized

        return self._mutate(token, False, mutation, re_reserve=True)

    def _check_stock_for_new_address(
        self, store: Store, channel: Channel, checkout: Checkout, shipping: Address | None, billing: Address | None
    ) -> None:
        if not checkout.lines:
            return
        requires_shipping = checkout_rules.requires_shipping(store, checkout)
        collection_point = (
            checkout.delivery.collection_point_warehouse_id
            if checkout.delivery.kind == DeliveryKind.COLLECTION_POINT
            else None
        )
        scope = stock_logic.scope(store, channel, shipping, billing, requires_shipping, collection_point)
        shortages = stock_logic.find_shortages(
            store,
            channel,
            scope,
            [(line.variant_id, line.quantity) for line in checkout.lines],
            self._ctx.now,
            checkout.id,
        )
        if shortages:
            raise stock_logic.shortage_error(shortages)

    def set_delivery(self, token: uuid.UUID, data: DeliveryInput) -> CheckoutView:
        external = None
        if data.external_method_id is not None:
            external = next(
                (m for m in self.external_shipping_methods(token) if f"{m.app_id}:{m.id}" == data.external_method_id),
                None,
            )

        def mutation(store: Store, checkout: Checkout, channel: Channel) -> None:
            given = sum(
                x is not None for x in (data.shipping_method_id, data.external_method_id, data.collection_point_id)
            )
            ensure(given <= 1, ErrorCodes.INVALID, "deliveryMethodId", "Choose one delivery method.")
            ensure(
                given == 0 or checkout_rules.requires_shipping(store, checkout),
                ErrorCodes.INVALID,
                "deliveryMethodId",
                "This checkout doesn't require shipping.",
            )

            if data.shipping_method_id is not None:
                ensure(
                    checkout.shipping_address is not None,
                    ErrorCodes.SHIPPING_ADDRESS_MISSING,
                    "shippingAddress",
                    "Set a shipping address first.",
                )
                pricing = self.price(store, checkout)
                context = ShippingContext(
                    channel,
                    checkout.shipping_address,
                    True,
                    pricing.subtotal_base,
                    pricing.weight_kg,
                    _product_ids(store, checkout),
                )
                ensure(
                    ShippingService.is_method_available(store, data.shipping_method_id, context),
                    ErrorCodes.DELIVERY_UNAVAILABLE,
                    "shippingMethodId",
                    "Shipping method is not applicable.",
                )
                checkout.delivery = DeliverySelection(
                    kind=DeliveryKind.SHIPPING_METHOD, shipping_method_id=data.shipping_method_id
                )
            elif data.external_method_id is not None:
                ensure(
                    external is not None,
                    ErrorCodes.DELIVERY_UNAVAILABLE,
                    "shippingMethodId",
                    "External shipping method is not available.",
                )
                checkout.delivery = DeliverySelection(kind=DeliveryKind.EXTERNAL_SHIPPING_METHOD, external=external)
            elif data.collection_point_id is not None:
                ensure(
                    any(
                        w.id == data.collection_point_id
                        for w in self._available_collection_points(store, checkout, channel)
                    ),
                    ErrorCodes.DELIVERY_UNAVAILABLE,
                    "collectionPointId",
                    "Collection point is not available.",
                )
                checkout.delivery = DeliverySelection(
                    kind=DeliveryKind.COLLECTION_POINT, collection_point_warehouse_id=data.collection_point_id
                )
            else:
                checkout.delivery = NO_DELIVERY

        return self._mutate(token, False, mutation, re_reserve=True)

    def available_shipping_methods(self, token: uuid.UUID) -> list[ShippingMethod]:
        def query(store: Store) -> tuple[list[ShippingMethod], ShippingAppRequest]:
            checkout, channel = self.load(store, token)
            pricing = self.price(store, checkout)
            methods = ShippingService.available_methods(
                store,
                ShippingContext(
                    channel,
                    checkout.shipping_address,
                    pricing.requires_shipping,
                    pricing.subtotal_base,
                    pricing.weight_kg,
                    _product_ids(store, checkout),
                ),
            )
            return methods, _shipping_request(checkout, channel, pricing)

        methods, request = self._ctx.db.write(query)
        excluded = self._filter_by_apps(request, [str(m.id) for m in methods])
        return [m for m in methods if str(m.id) not in excluded]

    def external_shipping_methods(self, token: uuid.UUID) -> list[ExternalShippingMethod]:
        ctx = self._ctx

        def prepare(store: Store) -> tuple[uuid.UUID, int, ShippingAppRequest | None]:
            checkout, channel = self.load(store, token)
            pricing = self.price(store, checkout)
            return (
                checkout.id,
                checkout.version,
                _shipping_request(checkout, channel, pricing) if pricing.requires_shipping else None,
            )

        checkout_id, version, request = ctx.db.write(prepare)
        if request is None:
            return []
        with self._cache_lock:
            cached = self._external_shipping_cache.get(checkout_id)
        if cached is not None and cached[0] == version and cached[1] > ctx.now:
            return cached[2]

        methods: list[ExternalShippingMethod] = []
        for app_id, handler in ctx.app_handlers.all(ShippingAppHandler):
            if not self._is_app_active(app_id):
                continue
            result = call_app(
                partial(handler.list_shipping_methods, request),
                ctx.options.sync_app_timeout,
                lambda offers: all(m.id and m.price >= 0 for m in offers),
            )
            if isinstance(result, AppCallOk):
                methods.extend(ExternalShippingMethod(app_id, m.id, m.name, m.price) for m in result.value)

        excluded = self._filter_by_apps(request, [f"{m.app_id}:{m.id}" for m in methods])
        filtered = [m for m in methods if f"{m.app_id}:{m.id}" not in excluded]
        with self._cache_lock:
            self._external_shipping_cache[checkout_id] = (
                version,
                ctx.now + ctx.options.shipping_app_cache_ttl,
                filtered,
            )
        return filtered

    def _filter_by_apps(self, request: ShippingAppRequest, ids: list[str]) -> set[str]:
        excluded: set[str] = set()
        if not ids:
            return excluded
        for app_id, handler in self._ctx.app_handlers.all(ShippingFilterAppHandler):
            if not self._is_app_active(app_id):
                continue
            result = call_app(
                partial(handler.exclude_shipping_methods, request, ids), self._ctx.options.sync_app_timeout
            )
            if isinstance(result, AppCallOk):  # filter app failed -> exclude nothing
                excluded.update(result.value)
        return excluded

    def available_collection_points(self, token: uuid.UUID) -> list[Warehouse]:
        def query(store: Store) -> list[Warehouse]:
            checkout, channel = self.load(store, token)
            return self._available_collection_points(store, checkout, channel)

        return self._ctx.db.write(query)

    def _available_collection_points(self, store: Store, checkout: Checkout, channel: Channel) -> list[Warehouse]:
        if not checkout_rules.requires_shipping(store, checkout):
            return []
        demand = [(line.variant_id, line.quantity) for line in checkout.lines]
        result = []
        for w in stock_logic.channel_warehouses(store, channel):
            if w.click_and_collect == ClickAndCollectOption.DISABLED:
                continue
            if w.click_and_collect == ClickAndCollectOption.LOCAL:
                scope = StockScope((w,), w.address.country)
            else:
                scope = StockScope(tuple(stock_logic.channel_warehouses(store, channel)), w.address.country)
            if not stock_logic.find_shortages(store, channel, scope, demand, self._ctx.now, checkout.id):
                result.append(w)
        return result

    def apply_voucher(self, token: uuid.UUID, code: str) -> CheckoutView:
        def mutation(store: Store, checkout: Checkout, channel: Channel) -> None:
            voucher, voucher_code = store.find_voucher_by_code(code)
            if voucher is None or voucher_code is None:
                raise DomainException(ErrorCodes.CODE_REJECTED, "promoCode", "Promo code is invalid.")
            pricing = self.price(store, checkout)
            user = store.users.get(checkout.user_id) if checkout.user_id is not None else None
            context = VoucherContext(
                channel,
                checkout.email,
                checkout.user_id,
                user is not None and user.is_staff and user.is_active,
                pricing.subtotal_base,
                sum(line.quantity for line in checkout.lines if not line.is_gift),
                checkout.shipping_address.country if checkout.shipping_address is not None else None,
            )
            error = validate_voucher(voucher, voucher_code, context, self._ctx.now)
            if error is not None:
                raise DomainException(error)
            checkout.voucher_code = voucher_code.code

        return self._mutate(token, False, mutation)

    def remove_voucher(self, token: uuid.UUID) -> CheckoutView:
        def mutation(_: Store, checkout: Checkout, __: Channel) -> None:
            checkout.voucher_code = None

        return self._mutate(token, False, mutation)

    def add_gift_card(self, token: uuid.UUID, code: str) -> CheckoutView:
        def mutation(store: Store, checkout: Checkout, channel: Channel) -> None:
            card = store.find_gift_card_by_code(code)
            if card is None or not card.is_usable_at(self._ctx.now, channel.currency):
                raise DomainException(
                    ErrorCodes.GIFT_CARD_REJECTED,
                    "promoCode",
                    "Gift card is invalid, inactive, expired or in another currency.",
                )
            if card.id not in checkout.gift_card_ids:
                checkout.gift_card_ids.append(card.id)

        return self._mutate(token, False, mutation)

    def remove_gift_card(self, token: uuid.UUID, gift_card_id: uuid.UUID) -> CheckoutView:
        def mutation(_: Store, checkout: Checkout, __: Channel) -> None:
            if gift_card_id in checkout.gift_card_ids:
                checkout.gift_card_ids.remove(gift_card_id)

        return self._mutate(token, False, mutation)

    def select_gift(self, token: uuid.UUID, variant_id: uuid.UUID) -> CheckoutView:
        def mutation(store: Store, checkout: Checkout, channel: Channel) -> None:
            previous = checkout.preferred_gift_variant_id
            checkout.preferred_gift_variant_id = variant_id
            reward = self._calculator.preview_order_reward(store, checkout)
            if reward is None or not reward.rule.is_gift_reward or reward.gift_variant_id != variant_id:
                checkout.preferred_gift_variant_id = previous
                raise DomainException(
                    ErrorCodes.GIFT_UNAVAILABLE, "variantId", "This gift is not available for the checkout."
                )

        return self._mutate(token, True, mutation)

    def attach_customer(self, principal: Principal, token: uuid.UUID) -> CheckoutView:
        def mutation(store: Store, checkout: Checkout, _: Channel) -> None:
            user = store.users[require_user(principal)]
            checkout.user_id = user.id
            checkout.email = user.email

        return self._mutate(token, False, mutation)

    def detach_customer(self, principal: Principal, token: uuid.UUID) -> CheckoutView:
        def mutation(_: Store, checkout: Checkout, __: Channel) -> None:
            ensure(
                checkout.user_id == principal.user_id or principal.has(Permission.HANDLE_CHECKOUTS),
                ErrorCodes.FORBIDDEN,
                None,
                "Not your checkout.",
            )
            checkout.user_id = None

        return self._mutate(token, False, mutation)

    def update_metadata(
        self,
        principal: Principal,
        token: uuid.UUID,
        upsert: Mapping[str, str] | None,
        remove: Sequence[str] | None = None,
        is_private: bool = False,
    ) -> CheckoutView:
        def mutation(_: Store, checkout: Checkout, __: Channel) -> None:
            if is_private:
                require_permission(principal, Permission.HANDLE_CHECKOUTS)
            patch(checkout.private_metadata if is_private else checkout.metadata, upsert, remove)

        return self._mutate(token, False, mutation)

    def set_tax_exemption(self, principal: Principal, token: uuid.UUID, exempt: bool) -> CheckoutView:
        require_permission(principal, Permission.MANAGE_TAXES)

        def mutation(_: Store, checkout: Checkout, __: Channel) -> None:
            checkout.tax_exempt = exempt

        return self._mutate(token, False, mutation)

    def available_payment_gateways(self, token: uuid.UUID) -> list[AvailablePaymentGateway]:
        def load(store: Store) -> tuple[uuid.UUID, str]:
            checkout, channel = self.load(store, token)
            return checkout.id, channel.currency

        checkout_id, currency = self._ctx.db.write(load)
        result: list[AvailablePaymentGateway] = []
        for app_id, handler in self._ctx.app_handlers.all(PaymentAppHandler):
            if not self._is_app_active(app_id):
                continue
            call = call_app(
                partial(handler.list_gateways, checkout_id, currency),
                self._ctx.options.sync_app_timeout,
                lambda gateways: all(g.id for g in gateways),
            )
            if isinstance(call, AppCallOk):
                result.extend(
                    AvailablePaymentGateway(app_id, g.id, g.name, g.config)
                    for g in call.value
                    if not g.currencies or currency in g.currencies
                )
        return result

    def load(self, store: Store, token: uuid.UUID) -> tuple[Checkout, Channel]:
        checkout = must_exist(store.find_checkout_by_token(token), "token", token)
        channel = store.channels[checkout.channel_id]
        ensure(channel.is_active, ErrorCodes.CHANNEL_INACTIVE, "channel", "Channel is inactive.")
        return checkout, channel

    def _mutate(
        self,
        token: uuid.UUID,
        lines_changed: bool,
        mutation: Callable[[Store, Checkout, Channel], object],
        re_reserve: bool = False,
    ) -> CheckoutView:
        def run(store: Store) -> CheckoutView:
            checkout, channel = self.load(store, token)
            mutation(store, checkout, channel)
            self._after_change(store, checkout, channel, lines_changed or re_reserve, prime_pricing=True)
            self._ctx.raise_event(EventTypes.CHECKOUT_UPDATED, payloads.checkout(checkout), channel.id)
            return self.view(store, checkout, channel)

        return self._ctx.db.write(run)

    def _after_change(
        self,
        store: Store,
        checkout: Checkout,
        channel: Channel,
        refresh_reservations: bool,
        prime_pricing: bool = False,
    ) -> None:
        ctx = self._ctx
        with self._cache_lock:
            self._price_cache.pop(checkout.id, None)
        pricing = (
            self._calculator.calculate(store, checkout)
            if prime_pricing and not any(line.is_gift for line in checkout.lines)
            else None
        )
        if pricing is None:
            gift_changed = self.sync_gift_line(store, checkout)
        else:
            gift_changed = self._sync_gift_line(checkout, None if not checkout.lines else pricing.order_reward)
        checkout.touch(ctx.now)
        if pricing is not None and not gift_changed:
            with self._cache_lock:
                self._price_cache[checkout.id] = (
                    checkout.version,
                    ctx.now + ctx.options.checkout_price_cache_ttl,
                    pricing,
                )
        if refresh_reservations or gift_changed:
            self.reserve(store, checkout, channel)

    def sync_gift_line(self, store: Store, checkout: Checkout) -> bool:
        return self._sync_gift_line(checkout, self._calculator.preview_order_reward(store, checkout))

    def _sync_gift_line(self, checkout: Checkout, reward: OrderRewardCandidate | None) -> bool:
        current = next((line for line in checkout.lines if line.is_gift), None)
        if reward is not None and reward.rule.is_gift_reward and reward.gift_variant_id is not None:
            if (
                current is not None
                and current.variant_id == reward.gift_variant_id
                and current.gift_promotion_rule_id == reward.rule.id
            ):
                return False
            checkout.lines[:] = [line for line in checkout.lines if not line.is_gift]
            checkout.lines.append(
                CheckoutLine(
                    variant_id=reward.gift_variant_id,
                    quantity=1,
                    is_gift=True,
                    gift_promotion_rule_id=reward.rule.id,
                    created_at=self._ctx.now,
                )
            )
            return True

        before = len(checkout.lines)
        checkout.lines[:] = [line for line in checkout.lines if not line.is_gift]
        return len(checkout.lines) < before

    def reserve(self, store: Store, checkout: Checkout, channel: Channel) -> None:
        minutes = (
            store.settings.reserve_stock_minutes_anonymous
            if checkout.user_id is None
            else store.settings.reserve_stock_minutes_authenticated
        )
        scope = checkout_rules.stock_scope_of(store, checkout, channel)
        stock_logic.reserve(
            store,
            channel,
            scope,
            checkout.id,
            [LineDemand(line.id, line.variant_id, line.quantity) for line in checkout.lines],
            self._ctx.now,
            timedelta(minutes=minutes or 0),
        )

    def _is_app_active(self, app_id: uuid.UUID) -> bool:
        def active(s: Store) -> bool:
            app = s.apps.get(app_id)
            return app is not None and app.is_active

        return self._ctx.db.read(active)


def _shipping_request(checkout: Checkout, channel: Channel, pricing: CheckoutPricing) -> ShippingAppRequest:
    return ShippingAppRequest(
        checkout.id,
        channel.id,
        channel.currency,
        checkout.shipping_address.country if checkout.shipping_address is not None else None,
        checkout.shipping_address.postal_code if checkout.shipping_address is not None else None,
        pricing.subtotal_base,
        pricing.weight_kg,
    )


def _product_ids(store: Store, checkout: Checkout) -> frozenset[uuid.UUID]:
    return frozenset(store.variants[line.variant_id].product_id for line in checkout.lines)


def _default_address(user: User | None, id_: uuid.UUID | None) -> Address | None:
    if user is None:
        return None
    saved = next((a for a in user.addresses if a.id == id_), None)
    return saved.address if saved is not None else None


def _same_metadata(existing: Mapping[str, str], incoming: Mapping[str, str] | None) -> bool:
    incoming = incoming or {}
    return len(existing) == len(incoming) and all(k in incoming and incoming[k] == v for k, v in existing.items())


def _scope_probe(checkout: Checkout, lines: list[CheckoutLine]) -> Checkout:
    return dataclasses.replace(checkout, lines=list(lines))
