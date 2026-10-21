from __future__ import annotations

import functools
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING

from ..accounts.model import UserAddress
from ..common.address import Address
from ..common.errors import DomainException, ErrorCodes, ErrorCollector
from ..common.metadata import patch
from ..common.money import Money
from ..discounts.model import GiftCardEvent, Voucher
from ..events import payloads
from ..events.domain_event import EventTypes
from ..inventory import stock_logic
from ..notifications import NotificationType, Notifier
from ..orders.model import Order, OrderDelivery, OrderLine, OrderStatus
from ..orders.view import OrderView
from ..payments import status as payment_status
from ..permissions import resolver
from ..persistence.store import Store
from . import rules as checkout_rules
from .calculator import CheckoutCalculator, CheckoutPricing
from .model import Checkout, DeliveryKind

if TYPE_CHECKING:
    from ..shop_context import ShopContext
    from .service import CheckoutService


@dataclass(frozen=True)
class CompletionResult:
    order: OrderView
    is_repeated: bool


class CheckoutCompletion:
    MAX_AUTO_COMPLETE_ATTEMPTS = 5

    def __init__(
        self, ctx: ShopContext, checkouts: CheckoutService, calculator: CheckoutCalculator, notifier: Notifier
    ):
        self._ctx = ctx
        self._checkouts = checkouts
        self._calculator = calculator
        self._notifier = notifier

    def complete(self, checkout_token: uuid.UUID) -> CompletionResult:
        def run(store: Store) -> tuple[CompletionResult, Order | None]:
            if store.find_checkout_by_token(checkout_token) is None:
                existing = store.orders.find_by_checkout_token(checkout_token)
                if existing is not None:
                    return CompletionResult(OrderView.masked(existing), True), None

            order = self._create_order(store, checkout_token)
            view = OrderView.full(order, [t for t in store.transactions.values() if t.order_id == order.id])
            return CompletionResult(view, False), order

        result, created = self._ctx.db.write(run)
        if created is not None:
            self._send_notifications(created)
        return result

    def _create_order(self, store: Store, checkout_token: uuid.UUID) -> Order:
        ctx = self._ctx
        checkout, channel = self._checkouts.load(store, checkout_token)
        now = ctx.now

        # Prices may have changed since the checkout was last viewed.
        self._checkouts.sync_gift_line(store, checkout)
        pricing = self._calculator.calculate(store, checkout)
        payment = payment_status.for_checkout(store, checkout.id, pricing.amount_to_pay)

        errors = ErrorCollector()
        if not checkout.lines:
            errors.add(ErrorCodes.CHECKOUT_EMPTY, "lines", "Cannot complete an empty checkout.")
        for problem in checkout_rules.problems(store, checkout, channel, now):
            errors.add(checkout_rules.problem_to_error(problem))
        email = (checkout.email or "").strip()
        if not email:
            errors.add(ErrorCodes.EMAIL_MISSING, "email", "Email is required.")
        if pricing.requires_shipping:
            if checkout.delivery.kind != DeliveryKind.COLLECTION_POINT and checkout.shipping_address is None:
                errors.add(ErrorCodes.SHIPPING_ADDRESS_MISSING, "shippingAddress", "Shipping address is required.")
            if checkout.delivery.kind == DeliveryKind.NONE:
                errors.add(ErrorCodes.DELIVERY_MISSING, "shippingMethod", "Delivery method is required.")
            elif not pricing.delivery_valid:
                errors.add(
                    ErrorCodes.DELIVERY_UNAVAILABLE,
                    "shippingMethod",
                    "Delivery method is no longer available.",
                )

        if pricing.voucher_error is not None:
            errors.add(pricing.voucher_error)
        for e in pricing.gift_card_errors:
            errors.add(e)

        if payment.covered < pricing.amount_to_pay and pricing.amount_to_pay > 0 and not channel.allow_unpaid_orders:
            errors.add(ErrorCodes.CHECKOUT_UNPAID, "payment", "The checkout is not fully paid.")
        errors.raise_if_any()

        # Build the allocation plan before touching the store.
        order_id = uuid.uuid4()
        order_lines = self._build_order_lines(store, pricing)
        scope = checkout_rules.stock_scope_of(store, checkout, channel)
        plan = stock_logic.plan_allocations(
            store,
            channel,
            scope,
            order_id,
            [(line.id, line.variant_id, line.quantity) for line in order_lines],
            now,
            checkout.id,
        )
        if plan.shortages:
            raise stock_logic.shortage_error(plan.shortages)

        # must be read before transactions are moved to the order
        covered_before = payment_status.total_covered_by_all_mechanisms(store, checkout.id)

        # for OUT_OF_STOCK events
        stock_before = {
            v: self._total_available(store, v) for v in dict.fromkeys(line.variant_id for line in order_lines)
        }

        shipping_address: Address | None
        if (
            checkout.delivery.kind == DeliveryKind.COLLECTION_POINT
            and checkout.delivery.collection_point_warehouse_id is not None
        ):
            shipping_address = store.warehouses[checkout.delivery.collection_point_warehouse_id].address
        else:
            shipping_address = checkout.shipping_address
        order = Order(
            id=order_id,
            number=store.next_order_number(),
            status=OrderStatus.UNFULFILLED if channel.automatically_confirm_orders else OrderStatus.UNCONFIRMED,
            channel_id=channel.id,
            currency=channel.currency,
            language_code=checkout.language_code,
            user_id=checkout.user_id,
            email=email,
            shipping_address=shipping_address if pricing.requires_shipping else None,
            billing_address=checkout.billing_address,
            delivery=_delivery_snapshot(store, checkout, pricing),
            voucher_id=pricing.voucher.id if pricing.voucher is not None else None,
            voucher_code=None if pricing.voucher is None else checkout.voucher_code,
            discounts=pricing.discounts,
            subtotal=pricing.subtotal,
            shipping_price=pricing.shipping,
            total=pricing.total,
            undiscounted_total=pricing.undiscounted_total,
            customer_note=checkout.customer_note,
            checkout_token=checkout.token,
            tax_exempt=checkout.tax_exempt,
            created_at=now,
        )
        order.lines.extend(order_lines)
        order.gift_cards.extend(g for g in pricing.gift_cards if g.amount > 0)
        patch(order.metadata, checkout.metadata)
        patch(order.private_metadata, checkout.private_metadata)
        store.orders.add(order.id, order)

        store.reservations.remove_for_checkout(checkout.id)
        stock_logic.apply(store, plan)

        _apply_voucher_usage(pricing.voucher, checkout)
        self._charge_gift_cards(store, order, checkout.user_id)

        for t in store.transactions.values():
            if t.checkout_id == checkout.id:
                t.checkout_id = None
                t.order_id = order.id

        for p in store.legacy_payments.values():
            if p.checkout_id == checkout.id:
                p.checkout_id = None
                p.order_id = order.id

        order_payment = payment_status.for_order(store, order.id, pricing.amount_to_pay)
        order.charge_status = order_payment.charge_status
        order.authorize_status = order_payment.authorize_status
        order.total_charged = order_payment.charged
        order.total_authorized = order_payment.authorized

        _save_addresses_to_book(store, checkout)
        store.checkouts.remove(checkout.id)

        ctx.raise_event(EventTypes.ORDER_CREATED, payloads.order(store, order), channel.id)
        if covered_before > pricing.amount_to_pay:
            order.is_overpaid_at_checkout = True
            ctx.raise_event(
                EventTypes.CHECKOUT_OVERPAID_AT_COMPLETION,
                payloads.overpayment(order, covered_before - pricing.amount_to_pay),
                channel.id,
            )

        for variant_id, before in stock_before.items():
            after = self._total_available(store, variant_id)
            if before > 0 and after == 0:
                ctx.raise_event(
                    EventTypes.PRODUCT_VARIANT_OUT_OF_STOCK, payloads.variant(store, store.variants[variant_id])
                )

        return order

    @staticmethod
    def _build_order_lines(store: Store, pricing: CheckoutPricing) -> list[OrderLine]:
        lines = []
        for p in pricing.lines:
            variant = store.variants[p.variant_id]
            product = store.products[variant.product_id]
            product_type = store.product_types[product.product_type_id]
            lines.append(
                OrderLine(
                    variant_id=variant.id,
                    product_name=product.name,
                    variant_name=variant.name,
                    sku=variant.sku,
                    quantity=p.quantity,
                    is_gift=p.is_gift,
                    is_shipping_required=product_type.is_shipping_required,
                    is_gift_card=product_type.is_gift_card,
                    undiscounted_unit_price=Money(p.base_unit_price, pricing.currency),
                    unit_price=p.unit_price,
                    undiscounted_total=p.undiscounted_total,
                    total=p.total,
                    tax_rate=p.tax_rate,
                    discounts=p.discounts,
                )
            )
        return lines

    def _charge_gift_cards(self, store: Store, order: Order, user_id: uuid.UUID | None) -> None:
        ctx = self._ctx
        for applied in order.gift_cards:
            card = store.gift_cards[applied.gift_card_id]
            amount = min(card.current_balance, applied.amount)
            card.current_balance -= amount
            card.events.append(GiftCardEvent(ctx.now, "SPENT", -amount, order.id, user_id))
            ctx.raise_event(EventTypes.GIFT_CARD_UPDATED, payloads.gift_card(card))

    def _total_available(self, store: Store, variant_id: uuid.UUID) -> int:
        return sum(stock_logic.available(store, s, self._ctx.now) for s in store.stocks_of_variant(variant_id))

    def _send_notifications(self, order: Order) -> None:
        def collect(store: Store) -> tuple[str, list[str], list[str]]:
            recipients = [
                u.email
                for u in store.users.values()
                if u.is_staff
                and u.is_active
                and u.subscribed_to_order_notifications
                and resolver.for_user(store, u).can_see_orders_of(order.channel_id)
            ]
            channel = store.channels.get(order.channel_id)
            return (
                channel.name if channel is not None else "",
                recipients,
                list(store.settings.additional_order_notification_emails),
            )

        channel_name, staff, extra = self._ctx.db.read(collect)

        data = {"number": str(order.number), "total": str(order.total.gross), "channel": channel_name}
        self._notifier.send(NotificationType.ORDER_CONFIRMATION, order.email, order.language_code, data)
        seen: set[str] = set()
        for email in [*staff, *extra]:
            if email.casefold() in seen:
                continue
            seen.add(email.casefold())
            self._notifier.send(NotificationType.STAFF_NEW_ORDER, email, None, data)

    def process_automatic_completions(self) -> int:
        ctx = self._ctx
        due = ctx.db.read(
            lambda store: [
                c.token
                for c in store.checkouts.values()
                if c.next_auto_complete_attempt_at is not None and c.next_auto_complete_attempt_at <= ctx.now
            ]
        )

        completed = 0
        for token in due:
            try:
                self.complete(token)
                completed += 1
            except DomainException as ex:
                ctx.db.write(functools.partial(self._record_auto_complete_failure, token=token, error=ex))

        return completed

    def _record_auto_complete_failure(self, store: Store, token: uuid.UUID, error: DomainException) -> None:
        ctx = self._ctx
        checkout = store.find_checkout_by_token(token)
        if checkout is None:
            return
        checkout.auto_complete_attempts += 1
        if checkout.auto_complete_attempts >= self.MAX_AUTO_COMPLETE_ATTEMPTS:
            checkout.next_auto_complete_attempt_at = None
            payload = payloads.checkout(checkout)
            payload["errors"] = [e.code for e in error.errors]
            ctx.raise_event(EventTypes.CHECKOUT_AUTOMATIC_COMPLETION_FAILED, payload, checkout.channel_id)
        else:
            checkout.next_auto_complete_attempt_at = ctx.now + timedelta(minutes=2**checkout.auto_complete_attempts)

    def delete_abandoned_checkouts(self) -> int:
        ctx = self._ctx

        def delete(store: Store) -> int:
            now = ctx.now

            def expired(c: Checkout) -> bool:
                age = now - c.last_change_at
                if not c.lines:
                    ttl = ctx.options.empty_checkout_ttl
                elif c.user_id is None:
                    ttl = ctx.options.anonymous_checkout_ttl
                else:
                    ttl = ctx.options.user_checkout_ttl
                return age > ttl and not has_accepted_payments(store, c.id)

            to_delete = [c for c in store.checkouts.values() if expired(c)]
            for checkout in to_delete:
                store.reservations.remove_for_checkout(checkout.id)
                store.checkouts.remove(checkout.id)
            return len(to_delete)

        return ctx.db.write(delete)


def has_accepted_payments(store: Store, checkout_id: uuid.UUID) -> bool:
    return any(
        t.checkout_id == checkout_id and t.charged_amount + t.authorized_amount > 0 for t in store.transactions.values()
    ) or any(p.checkout_id == checkout_id and p.covered > 0 for p in store.legacy_payments.values())


def _delivery_snapshot(store: Store, checkout: Checkout, pricing: CheckoutPricing) -> OrderDelivery | None:
    d = checkout.delivery
    if d.kind == DeliveryKind.SHIPPING_METHOD:
        m = store.find_shipping_method(d.shipping_method_id)
        if m is not None:
            return OrderDelivery("shipping", str(m.id), m.name, pricing.shipping, None)
    elif d.kind == DeliveryKind.EXTERNAL_SHIPPING_METHOD and d.external is not None:
        e = d.external
        return OrderDelivery("external", f"{e.app_id}:{e.id}", e.name, pricing.shipping, None)
    elif d.kind == DeliveryKind.COLLECTION_POINT and d.collection_point_warehouse_id is not None:
        w = store.warehouses.get(d.collection_point_warehouse_id)
        if w is not None:
            return OrderDelivery("collection_point", str(w.id), w.name, pricing.shipping, w.id)
    return None


def _apply_voucher_usage(voucher: Voucher | None, checkout: Checkout) -> None:
    if voucher is None:
        return
    code = next(c for c in voucher.codes if c.code == checkout.voucher_code)
    code.used += 1
    if voucher.apply_once_per_customer and checkout.email is not None:
        voucher.customer_emails_used.add(checkout.email)


def _save_addresses_to_book(store: Store, checkout: Checkout) -> None:
    user = store.users.get(checkout.user_id) if checkout.user_id is not None else None
    if user is None:
        return
    for address in (checkout.shipping_address, checkout.billing_address):
        if address is None:
            continue
        if any(a.address == address for a in user.addresses):
            continue
        saved = UserAddress(uuid.uuid4(), address)
        user.addresses.append(saved)
        if address == checkout.shipping_address and user.default_shipping_address_id is None:
            user.default_shipping_address_id = saved.id
        if address == checkout.billing_address and user.default_billing_address_id is None:
            user.default_billing_address_id = saved.id
