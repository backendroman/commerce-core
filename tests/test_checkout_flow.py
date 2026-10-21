from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from sales_api.apps.handlers import PaymentAppOutcome, PaymentAppResponse
from sales_api.channels.model import PaymentAction
from sales_api.channels.service import ChannelCreateInput, ChannelUpdateInput
from sales_api.checkout.service import CheckoutCreateInput, DeliveryInput
from sales_api.common.errors import DomainException, ErrorCodes
from sales_api.discounts.model import DiscountValueType, VoucherChannelListing, VoucherType
from sales_api.discounts.service import GiftCardInput, VoucherInput
from sales_api.events.domain_event import EventTypes
from sales_api.inventory.model import ClickAndCollectOption
from sales_api.inventory.service import StockInput, WarehouseCreateInput
from sales_api.notifications import NotificationType
from sales_api.orders.model import OrderStatus
from sales_api.payments.legacy_gateways import DummyGateway
from sales_api.payments.model import ChargeStatus, TransactionEventType
from sales_api.payments.service import PaymentStartInput, TransactionEventInput, TransactionInput
from sales_api.payments.status import CheckoutPaymentMechanism
from sales_api.permissions import resolver
from sales_api.permissions.model import ANONYMOUS, Permission
from sales_api.permissions.staff_service import GroupInput
from sales_api.shipping.model import ShippingMethodChannelListing
from sales_api.shipping.service import ShippingMethodInput
from support import BERLIN, PARIS, WARSAW, D, FakeShippingApp, expect_error, line


def test_guest_checkout_voucher_3ds(f):
    shirt = f.product("Shirt", 30, stock=5)
    mug = f.product("Mug", 20, stock=5)
    f.shop.discounts.create_voucher(
        f.owner,
        VoucherInput(
            name="Spring",
            type=VoucherType.ENTIRE_ORDER,
            codes=("SPRING10",),
            channel_listings=(VoucherChannelListing(f.channel.id, D(10)),),
        ),
    )
    app = f.install_payment_app()
    app.on_initialize = lambda r: PaymentAppResponse(
        PaymentAppOutcome.CHARGE_NEEDS_ACTION, r.amount, "psp-3ds", {"redirect": "https://bank/3ds"}
    )

    checkout = f.new_checkout((shirt, 2), (mug, 1))
    f.ready_for_payment(checkout.token)
    view = f.shop.checkouts.apply_voucher(checkout.token, "spring10")

    assert view.pricing.subtotal.gross.amount == 72
    assert view.pricing.total.gross.amount == 82

    init = f.shop.payments.start_payment(PaymentStartInput(checkout_token=checkout.token, app_id=app.app_id))
    assert init.event.type == TransactionEventType.CHARGE_NEEDS_ACTION
    assert init.data["redirect"] == "https://bank/3ds"
    assert init.transaction.pending_charge_amount == 82

    app.on_process = lambda r: PaymentAppResponse(PaymentAppOutcome.CHARGE_SETTLED, D(82), "psp-3ds")
    f.shop.payments.continue_payment(init.transaction.id, {"3ds": "ok"})

    result = f.shop.completion.complete(checkout.token)

    order = result.order
    assert not result.is_repeated
    assert order.status == OrderStatus.UNFULFILLED
    assert order.charge_status == ChargeStatus.FULL
    assert order.total.gross.amount == 82
    assert order.email == "mark@example.com"

    def check(store):
        assert store.stocks[(f.warehouse.id, shirt.id)].quantity_allocated == 2
        assert store.stocks[(f.warehouse.id, mug.id)].quantity_allocated == 1
        assert store.find_voucher_by_code("SPRING10")[0].total_used == 1
        assert len(store.checkouts) == 0

    f.shop.context.db.read(check)
    assert any(m.type == NotificationType.ORDER_CONFIRMATION for m in f.shop.context.mail.to("mark@example.com"))


def test_click_and_collect(f):
    paris = f.shop.inventory.create_warehouse(
        f.owner,
        WarehouseCreateInput(
            name="Paris pickup", address=PARIS, click_and_collect=ClickAndCollectOption.ALL_WAREHOUSES
        ),
    )
    f.shop.channels.update(f.owner, f.channel.id, ChannelUpdateInput(warehouse_ids=(f.warehouse.id, paris.id)))
    f.shop.taxes.set_country_default_rate(f.owner, "FR", D(20))
    f.shop.taxes.set_country_default_rate(f.owner, "PL", D(23))
    item = f.product("Lamp", 120, stock=3)  # only in Berlin

    checkout = f.new_checkout((item, 1))
    f.shop.checkouts.set_billing_address(checkout.token, WARSAW)
    points = f.shop.checkouts.available_collection_points(checkout.token)
    assert any(w.id == paris.id for w in points)

    view = f.shop.checkouts.set_delivery(checkout.token, DeliveryInput(collection_point_id=paris.id))
    assert view.pricing.total.gross.amount == 120
    assert view.pricing.total.net.amount == 100
    assert view.pricing.tax_country == "FR"

    f.pay_in_full(checkout.token)
    order = f.shop.completion.complete(checkout.token).order

    assert order.delivery.kind == "collection_point"
    assert order.shipping_address.country == "FR"
    assert (
        f.shop.context.db.read(
            lambda s: len(
                [
                    a
                    for a in s.allocations
                    if a.order_id == order.id and a.warehouse_id == f.warehouse.id and a.quantity == 1
                ]
            )
        )
        == 1
    )


def test_mixed_gift_card_and_card(f):
    item = f.product("Boots", 90)
    card = f.shop.discounts.issue_gift_card(f.owner, GiftCardInput(balance=D(50), currency="EUR", code="GIFT-0001"))
    checkout = f.new_checkout((item, 1))
    f.ready_for_payment(checkout.token)

    view = f.shop.checkouts.add_gift_card(checkout.token, "gift-0001")
    assert view.pricing.total.gross.amount == 100
    assert view.pricing.amount_to_pay == 50

    f.pay_in_full(checkout.token)
    order = f.shop.completion.complete(checkout.token).order

    assert order.charge_status == ChargeStatus.FULL

    def check(store):
        assert store.gift_cards[card.id].current_balance == 0
        assert any(e.type == "SPENT" and e.balance_delta == -50 for e in store.gift_cards[card.id].events)

    f.shop.context.db.read(check)


def test_race_for_last_unit(f):
    item = f.product("Last one", 10, stock=1)
    app = f.install_payment_app()
    tokens = []
    for _ in range(2):
        c = f.new_checkout((item, 1))
        f.ready_for_payment(c.token)
        f.pay_in_full(c.token, app)
        tokens.append(c.token)

    def complete(token):
        try:
            return f.shop.completion.complete(token).order, None
        except DomainException as ex:
            return None, ex

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(complete, tokens))

    assert len([o for o, _ in outcomes if o is not None]) == 1
    [failure] = [e for _, e in outcomes if e is not None]
    assert failure.has(ErrorCodes.INSUFFICIENT_STOCK)
    assert f.shop.context.db.read(lambda s: s.stocks[(f.warehouse.id, item.id)].quantity_allocated) == 1


def test_repeated_completion_returns_masked_order(f):
    item = f.product("Book", 15)
    checkout = f.new_checkout((item, 1))
    f.ready_for_payment(checkout.token)
    f.pay_in_full(checkout.token)

    first = f.shop.completion.complete(checkout.token)
    second = f.shop.completion.complete(checkout.token)

    assert first.order.id == second.order.id
    assert not first.is_repeated
    assert first.order.email == "mark@example.com"
    assert first.order.shipping_address.first_name == "Max"
    assert second.is_repeated
    assert second.order.email == "m***@example.com"
    assert second.order.shipping_address.first_name == "M**"
    assert second.order.transactions is None
    assert f.shop.context.db.read(lambda s: len(s.orders)) == 1


def test_manual_confirmation_channel_creates_unconfirmed_order(f):
    f.shop.channels.update(f.owner, f.channel.id, ChannelUpdateInput(automatically_confirm_orders=False))
    item = f.product("Pen", 2)
    checkout = f.new_checkout((item, 1))
    f.ready_for_payment(checkout.token)
    f.pay_in_full(checkout.token)

    assert f.shop.completion.complete(checkout.token).order.status == OrderStatus.UNCONFIRMED


def test_edit_after_payment_blocks_completion(f):
    item = f.product("Cup", 10)
    checkout = f.new_checkout((item, 1))
    f.ready_for_payment(checkout.token)
    f.pay_in_full(checkout.token)
    assert f.shop.checkouts.get(checkout.token).payment.charge_status == ChargeStatus.FULL

    view = f.shop.checkouts.add_lines(ANONYMOUS, checkout.token, [line(item, 1)])

    assert view.payment.charge_status == ChargeStatus.PARTIAL
    expect_error(lambda: f.shop.completion.complete(checkout.token), ErrorCodes.CHECKOUT_UNPAID)


def test_unpaid_orders_allowed(f):
    f.shop.channels.update(f.owner, f.channel.id, ChannelUpdateInput(allow_unpaid_orders=True))
    card = f.shop.discounts.issue_gift_card(f.owner, GiftCardInput(balance=D(5), currency="EUR"))
    item = f.product("Cap", 15)
    checkout = f.new_checkout((item, 1))
    f.ready_for_payment(checkout.token)
    f.shop.checkouts.add_gift_card(checkout.token, card.code)

    order = f.shop.completion.complete(checkout.token).order

    assert order.charge_status == ChargeStatus.NONE
    assert f.shop.context.db.read(lambda s: s.gift_cards[card.id].current_balance) == 0
    assert any(m.type == NotificationType.ORDER_CONFIRMATION for m in f.shop.context.mail.sent)


def test_app_decides_actual_action(f):
    item = f.product("Chair", 50)
    app = f.install_payment_app()
    app.on_initialize = lambda r: PaymentAppResponse(PaymentAppOutcome.CHARGE_SETTLED, r.amount, "psp-1")
    checkout = f.new_checkout((item, 1))
    f.ready_for_payment(checkout.token)

    result = f.shop.payments.start_payment(
        PaymentStartInput(checkout_token=checkout.token, app_id=app.app_id, action=PaymentAction.HOLD)
    )

    assert result.transaction.charged_amount == 60
    assert result.transaction.authorized_amount == 0
    assert f.shop.checkouts.get(checkout.token).payment.charge_status == ChargeStatus.FULL


def test_overpaid_with_legacy_payment(f):
    listener = f.shop.apps.create_local_app(f.owner, "listener", []).app
    f.shop.apps.subscribe(
        f.owner,
        listener.id,
        "https://hooks.example.com/overpaid",
        [EventTypes.CHECKOUT_OVERPAID_AT_COMPLETION],
        "{ order { number } overpaidAmount { amount currency } }",
    )
    item = f.product("Desk", 90)
    checkout = f.new_checkout((item, 1))
    f.ready_for_payment(checkout.token)  # total 100
    f.shop.payments.create_legacy_payment(checkout.token, DummyGateway.GATEWAY_ID, D(50))
    f.pay_in_full(checkout.token)

    view = f.shop.checkouts.get(checkout.token)
    assert view.payment.mechanism == CheckoutPaymentMechanism.TRANSACTIONS
    assert view.payment.charge_status == ChargeStatus.FULL

    order = f.shop.completion.complete(checkout.token).order

    assert order.charge_status == ChargeStatus.OVERCHARGED
    assert f.shop.context.db.read(lambda s: s.orders[order.id].is_overpaid_at_checkout)
    f.run_jobs()
    [message] = [r for r in f.webhooks.sent if r.headers["X-Event-Type"] == EventTypes.CHECKOUT_OVERPAID_AT_COMPLETION]
    assert message.body == '{"order":{"number":1},"overpaidAmount":{"amount":50,"currency":"EUR"}}'


def test_legacy_payment_completion(f):
    item = f.product("Sofa", 190)
    checkout = f.new_checkout((item, 1))
    f.ready_for_payment(checkout.token)
    f.shop.payments.create_legacy_payment(checkout.token, DummyGateway.GATEWAY_ID, D(50))
    f.shop.payments.create_legacy_payment(checkout.token, DummyGateway.GATEWAY_ID, D(200))

    view = f.shop.checkouts.get(checkout.token)
    assert view.payment.mechanism == CheckoutPaymentMechanism.LEGACY_PAYMENT
    assert view.payment.active_legacy_payment.amount == 200

    order = f.shop.completion.complete(checkout.token).order
    assert order.charge_status == ChargeStatus.OVERCHARGED


def test_failed_completion_leaves_no_traces(f):
    item = f.product("Vase", 40, stock=2)
    card = f.shop.discounts.issue_gift_card(f.owner, GiftCardInput(balance=D(10), currency="EUR"))
    f.shop.discounts.create_voucher(
        f.owner,
        VoucherInput(
            name="V",
            type=VoucherType.ENTIRE_ORDER,
            codes=("SAVE5",),
            value_type=DiscountValueType.FIXED,
            channel_listings=(VoucherChannelListing(f.channel.id, D(5)),),
        ),
    )
    checkout = f.new_checkout((item, 2))
    f.ready_for_payment(checkout.token)
    f.shop.checkouts.apply_voucher(checkout.token, "SAVE5")
    f.shop.checkouts.add_gift_card(checkout.token, card.code)
    f.pay_in_full(checkout.token)
    f.shop.inventory.update_stocks(f.owner, [StockInput(item.id, f.warehouse.id, 1)])

    expect_error(lambda: f.shop.completion.complete(checkout.token))

    def check(store):
        assert len(store.orders) == 0
        assert store.allocations == []
        assert store.find_voucher_by_code("SAVE5")[0].total_used == 0
        assert store.gift_cards[card.id].current_balance == 10
        assert len(store.checkouts) == 1

    f.shop.context.db.read(check)


def test_completion_validates_email_address_and_delivery(f):
    item = f.product("Hat", 5)
    checkout = f.shop.checkouts.create(ANONYMOUS, CheckoutCreateInput(channel_id=f.channel.id, lines=(line(item, 1),)))

    ex = expect_error(lambda: f.shop.completion.complete(checkout.token))

    assert ex.has(ErrorCodes.EMAIL_MISSING)
    assert ex.has(ErrorCodes.SHIPPING_ADDRESS_MISSING)
    assert ex.has(ErrorCodes.DELIVERY_MISSING)
    assert ex.has(ErrorCodes.CHECKOUT_UNPAID)


def test_order_numbers_are_global_across_channels(f):
    second = f.shop.channels.create(
        f.owner,
        ChannelCreateInput(
            name="Second",
            currency="EUR",
            default_country="DE",
            warehouse_ids=(f.warehouse.id,),
            shipping_zone_ids=(f.zone.id,),
        ),
    )
    f.shop.shipping.create_method(
        f.owner,
        ShippingMethodInput(
            zone_id=f.zone.id, name="Second std", channel_listings=(ShippingMethodChannelListing(second.id, D(0)),)
        ),
    )
    f.shop.channels.update(f.owner, second.id, ChannelUpdateInput(allow_unpaid_orders=True))
    f.shop.channels.update(f.owner, f.channel.id, ChannelUpdateInput(allow_unpaid_orders=True))

    a = f.product("A", 1)
    b = f.product("B", 1, channel_id=second.id)
    c1 = f.new_checkout((a, 1))
    f.ready_for_payment(c1.token)
    c2 = f.shop.checkouts.create(
        ANONYMOUS,
        CheckoutCreateInput(channel_id=second.id, email="x@y.zz", lines=(line(b, 1),), shipping_address=BERLIN),
    )
    [method] = f.shop.checkouts.available_shipping_methods(c2.token)
    f.shop.checkouts.set_delivery(c2.token, DeliveryInput(shipping_method_id=method.id))

    assert f.shop.completion.complete(c1.token).order.number == 1
    assert f.shop.completion.complete(c2.token).order.number == 2


def test_auto_completion_retries(f):
    f.shop.channels.update(f.owner, f.channel.id, ChannelUpdateInput(automatically_complete_fully_paid_checkouts=True))
    item = f.product("Kettle", 30)
    ok = f.new_checkout((item, 1))
    f.ready_for_payment(ok.token)
    f.pay_in_full(ok.token)

    f.shop.run_scheduled_jobs()
    assert f.shop.context.db.read(lambda s: len(s.orders)) == 1

    # paid but can't complete (no delivery): five attempts, then an event
    app = f.shop.apps.create_local_app(f.owner, "listener", []).app
    f.shop.apps.subscribe(
        f.owner,
        app.id,
        "https://hooks.example.com/x",
        [EventTypes.CHECKOUT_AUTOMATIC_COMPLETION_FAILED],
        "{ checkout { id } errors }",
    )
    broken = f.new_checkout((item, 1))
    f.shop.checkouts.set_shipping_address(broken.token, BERLIN)
    pay_app = f.install_payment_app("pay2")
    f.shop.payments.start_payment(PaymentStartInput(checkout_token=broken.token, app_id=pay_app.app_id, amount=D(30)))

    for _ in range(6):
        f.shop.run_scheduled_jobs()
        f.clock.advance(timedelta(hours=1))

    f.shop.run_scheduled_jobs()
    assert (
        len(
            [r for r in f.webhooks.sent if r.headers["X-Event-Type"] == EventTypes.CHECKOUT_AUTOMATIC_COMPLETION_FAILED]
        )
        == 1
    )


def test_delete_abandoned_checkouts(f):
    item = f.product("Plate", 3)
    anonymous = f.new_checkout((item, 1))
    paid = f.new_checkout((item, 1))
    f.ready_for_payment(paid.token)
    f.pay_in_full(paid.token)
    empty = f.new_checkout()

    f.clock.advance(timedelta(hours=7))
    assert f.shop.completion.delete_abandoned_checkouts() == 1  # the empty one
    f.clock.advance(timedelta(days=31))
    assert f.shop.completion.delete_abandoned_checkouts() == 1  # anonymous with lines

    def check(store):
        assert store.find_checkout_by_token(empty.token) is None
        assert store.find_checkout_by_token(anonymous.token) is None
        assert store.find_checkout_by_token(paid.token) is not None

    f.shop.context.db.read(check)


def test_inactive_channel_makes_checkout_unavailable(f):
    item = f.product("Fork", 1)
    checkout = f.new_checkout((item, 1))
    f.shop.channels.update(f.owner, f.channel.id, ChannelUpdateInput(is_active=False))

    expect_error(lambda: f.shop.checkouts.get(checkout.token), ErrorCodes.CHANNEL_INACTIVE)


def test_channel_delete_with_orders(f):
    f.shop.channels.update(f.owner, f.channel.id, ChannelUpdateInput(allow_unpaid_orders=True))
    item = f.product("Spoon", 1)
    checkout = f.new_checkout((item, 1))
    f.ready_for_payment(checkout.token)
    order = f.shop.completion.complete(checkout.token).order
    usd = f.shop.channels.create(f.owner, ChannelCreateInput(name="US", currency="USD", default_country="US"))
    eur = f.shop.channels.create(f.owner, ChannelCreateInput(name="EU2", currency="EUR", default_country="FR"))

    expect_error(lambda: f.shop.channels.delete(f.owner, f.channel.id), ErrorCodes.INVALID)
    expect_error(lambda: f.shop.channels.delete(f.owner, f.channel.id, usd.id), ErrorCodes.CURRENCY_MISMATCH)

    f.shop.channels.delete(f.owner, f.channel.id, eur.id)
    assert f.shop.context.db.read(lambda s: s.orders[order.id].channel_id) == eur.id


def test_transaction_currency_and_duplicate_events(f):
    item = f.product("Rug", 50)
    checkout = f.new_checkout((item, 1))
    f.ready_for_payment(checkout.token)
    app = f.shop.apps.create_local_app(f.owner, "psp", [Permission.HANDLE_PAYMENTS]).app
    app_principal = resolver.for_app(app)

    expect_error(
        lambda: f.shop.payments.create_transaction(
            app_principal, TransactionInput(checkout_token=checkout.token, currency="USD")
        ),
        ErrorCodes.CURRENCY_MISMATCH,
    )

    t = f.shop.payments.create_transaction(
        app_principal, TransactionInput(checkout_token=checkout.token, currency="EUR", provider_reference="ext-1")
    )
    ev = TransactionEventInput(
        transaction_id=t.id, type=TransactionEventType.CHARGE_SETTLED, amount=D(60), provider_reference="ext-1"
    )
    f.shop.payments.report_event(app_principal, ev)
    f.shop.payments.report_event(app_principal, ev)

    assert f.shop.context.db.read(lambda s: s.transactions[t.id].charged_amount) == 60
    expect_error(lambda: f.shop.payments.report_event(ANONYMOUS, ev))


def test_idempotency_key(f):
    item = f.product("Mat", 10)
    checkout = f.new_checkout((item, 1))
    f.ready_for_payment(checkout.token)
    app = f.install_payment_app()

    first = f.shop.payments.start_payment(
        PaymentStartInput(checkout_token=checkout.token, app_id=app.app_id, idempotency_key="k1")
    )
    second = f.shop.payments.start_payment(
        PaymentStartInput(checkout_token=checkout.token, app_id=app.app_id, idempotency_key="k1")
    )

    assert first.transaction.id == second.transaction.id
    assert app.initialize_calls == 1


def test_failing_payment_app(f):
    item = f.product("Tray", 10)
    checkout = f.new_checkout((item, 1))
    good = f.install_payment_app("good")
    bad = f.install_payment_app("bad")
    bad.fail_listing = True

    gateways = f.shop.checkouts.available_payment_gateways(checkout.token)
    assert len(gateways) == 1
    assert gateways[0].app_id == good.app_id

    def boom(_):
        raise RuntimeError("boom")

    bad.on_initialize = boom
    expect_error(
        lambda: f.shop.payments.start_payment(
            PaymentStartInput(checkout_token=checkout.token, app_id=bad.app_id, amount=D(5))
        ),
        ErrorCodes.PAYMENT_FAILED,
    )
    assert f.shop.context.db.read(
        lambda s: any(e.type == TransactionEventType.CHARGE_DECLINED for t in s.transactions.values() for e in t.events)
    )


def test_stored_payment_methods_and_tokenization(f):
    app = f.install_payment_app()
    user = f.shop.accounts.register("saver@example.com", "Password123")
    principal = f.principal_of(user.id)

    assert len(f.shop.payments.stored_payment_methods(principal, f.channel.id)) == 1
    f.shop.payments.delete_stored_payment_method(principal, app.app_id, "pm_1")
    expect_error(lambda: f.shop.payments.delete_stored_payment_method(principal, app.app_id, "pm_x"))
    assert (
        f.shop.payments.initialize_tokenization(principal, app.app_id, f.channel.id, None).result
        == "ADDITIONAL_ACTION_REQUIRED"
    )
    assert f.shop.payments.process_tokenization(principal, app.app_id, "s1", None).payment_method_id == "pm_2"


def test_staff_notifications_respect_channel_restriction(f):
    f.shop.channels.update(f.owner, f.channel.id, ChannelUpdateInput(allow_unpaid_orders=True))
    other = f.shop.channels.create(f.owner, ChannelCreateInput(name="B", currency="EUR", default_country="DE"))
    restricted = f.staff_with("b-only@example.org")
    f.shop.staff.create_group(
        f.owner,
        GroupInput(
            name="B orders",
            permissions=(Permission.MANAGE_ORDERS,),
            user_ids=(restricted.user_id,),
            restricted_to_channels=True,
            channel_ids=(other.id,),
        ),
    )
    all_orders = f.staff_with("all@example.org", Permission.MANAGE_ORDERS)
    f.shop.staff.set_order_notification_subscription(f.principal_of(restricted.user_id), True)
    f.shop.staff.set_order_notification_subscription(all_orders, True)
    f.settings.additional_order_notification_emails.append("ops@partner.example.com")

    item = f.product("Bowl", 4)
    checkout = f.new_checkout((item, 1))
    f.ready_for_payment(checkout.token)
    f.shop.completion.complete(checkout.token)

    staff_mails = [m.to for m in f.shop.context.mail.sent if m.type == NotificationType.STAFF_NEW_ORDER]
    assert "all@example.org" in staff_mails
    assert "ops@partner.example.com" in staff_mails
    assert "b-only@example.org" not in staff_mails


def test_external_shipping_methods(f):
    app = f.shop.apps.create_local_app(f.owner, "carrier", []).app
    carrier = FakeShippingApp()
    f.shop.context.app_handlers.register(app.id, carrier)
    item = f.product("Crate", 70)
    checkout = f.new_checkout((item, 1))
    f.shop.checkouts.set_shipping_address(checkout.token, PARIS)

    [dhl] = f.shop.checkouts.external_shipping_methods(checkout.token)
    view = f.shop.checkouts.set_delivery(checkout.token, DeliveryInput(external_method_id=f"{dhl.app_id}:{dhl.id}"))
    assert view.pricing.total.gross.amount == 95

    carrier.fail = True
    f.shop.checkouts.set_customer_note(checkout.token, "invalidate cache")
    assert f.shop.checkouts.external_shipping_methods(checkout.token) == []
