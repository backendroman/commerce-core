import dataclasses
from datetime import timedelta

from sales_api.catalog.model import PreorderSettings
from sales_api.catalog.service import ProductChannelListingInput, VariantUpdateInput
from sales_api.channels.model import AllocationStrategy
from sales_api.channels.service import ChannelCreateInput, ChannelUpdateInput
from sales_api.checkout.rules import CheckoutProblemType
from sales_api.checkout.service import CheckoutCreateInput, CheckoutLineInput, CheckoutLineUpdate, DeliveryInput
from sales_api.common.address import Address
from sales_api.common.errors import ErrorCodes
from sales_api.events.domain_event import EventTypes
from sales_api.inventory import stock_logic
from sales_api.inventory.model import ClickAndCollectOption
from sales_api.inventory.service import StockInput, WarehouseCreateInput
from sales_api.permissions.model import ANONYMOUS
from sales_api.shipping.model import (
    PostalCodeRange,
    PostalCodeRuleMode,
    ShippingMethodChannelListing,
    ShippingMethodType,
)
from sales_api.shipping.service import ShippingMethodInput, ShippingZoneInput
from support import BERLIN, PARIS, WARSAW, D, ShopFixture, expect_error, line


def paris_warehouse_only_serving_france(f: ShopFixture):
    paris = f.shop.inventory.create_warehouse(f.owner, WarehouseCreateInput(name="Paris", address=PARIS))
    f.shop.shipping.create_zone(
        f.owner,
        ShippingZoneInput(name="France", countries=("FR",), warehouse_ids=(paris.id,), channel_ids=(f.channel.id,)),
    )
    f.shop.channels.update(f.owner, f.channel.id, ChannelUpdateInput(warehouse_ids=(f.warehouse.id, paris.id)))
    return paris


def test_available_quantity(f):
    f.settings.reserve_stock_minutes_anonymous = 15
    item = f.product("Item", 5, stock=5)
    first = f.new_checkout((item, 3))

    # other shopper sees 2
    second = f.new_checkout()
    expect_error(
        lambda: f.shop.checkouts.add_lines(ANONYMOUS, second.token, [line(item, 3)]), ErrorCodes.INSUFFICIENT_STOCK
    )
    f.shop.checkouts.add_lines(ANONYMOUS, second.token, [line(item, 2)])

    f.shop.checkouts.update_lines(
        first.token, [CheckoutLineUpdate(f.shop.checkouts.get(first.token).checkout.lines[0].id, 3)]
    )

    f.clock.advance(timedelta(minutes=16))
    third = f.new_checkout((item, 5))
    assert len(third.lines) == 1

    # stock set below allocated, available clamps to zero
    def allocate(s):
        s.stocks[(f.warehouse.id, item.id)].quantity_allocated = 4

    f.shop.context.db.write(allocate)
    f.shop.inventory.update_stocks(f.owner, [StockInput(item.id, f.warehouse.id, 1)])
    assert (
        f.shop.context.db.read(lambda s: stock_logic.available(s, s.stocks[(f.warehouse.id, item.id)], f.clock.utc_now))
        == 0
    )


def test_reservations_disabled_stock_still_checked(f):
    item = f.product("Item", 5, stock=2)
    f.new_checkout((item, 2))
    assert f.shop.context.db.read(lambda s: len(s.reservations)) == 0
    expect_error(lambda: f.new_checkout((item, 3)))


def test_reservation_duration_depends_on_authentication(f):
    f.settings.reserve_stock_minutes_anonymous = 5
    f.settings.reserve_stock_minutes_authenticated = 30
    item = f.product("Item", 5)
    user = f.shop.accounts.register("buyer@example.com", "Password123")

    def activate(s):
        s.users[user.id].is_active = True

    f.shop.context.db.write(activate)

    guest = f.new_checkout((item, 1))
    mine = f.shop.checkouts.create(
        f.principal_of(user.id), CheckoutCreateInput(channel_id=f.channel.id, lines=(line(item, 1),))
    )

    def check(s):
        [g] = [r for r in s.reservations if r.checkout_id == guest.id]
        [m] = [r for r in s.reservations if r.checkout_id == mine.id]
        assert g.reserved_until == f.clock.utc_now + timedelta(minutes=5)
        assert m.reserved_until == f.clock.utc_now + timedelta(minutes=30)

    f.shop.context.db.read(check)


def test_stock_country_follows_address(f):
    paris = paris_warehouse_only_serving_france(f)
    item = f.product("French cheese", 7, stock=5, warehouse_id=paris.id)
    checkout = f.new_checkout()

    # no address, so the channel default country DE applies, and the Paris warehouse doesn't serve DE
    expect_error(
        lambda: f.shop.checkouts.add_lines(ANONYMOUS, checkout.token, [line(item, 1)]), ErrorCodes.INSUFFICIENT_STOCK
    )

    f.shop.checkouts.set_shipping_address(checkout.token, PARIS)
    f.shop.checkouts.add_lines(ANONYMOUS, checkout.token, [line(item, 1)])

    expect_error(lambda: f.shop.checkouts.set_shipping_address(checkout.token, BERLIN), ErrorCodes.INSUFFICIENT_STOCK)
    assert f.shop.checkouts.get(checkout.token).checkout.shipping_address.country == "FR"


def test_reservations_move_with_address(f):
    f.settings.reserve_stock_minutes_anonymous = 20
    paris = paris_warehouse_only_serving_france(f)
    item = f.product("Item", 7, stock=5, warehouse_id=paris.id)
    f.shop.inventory.update_stocks(f.owner, [StockInput(item.id, f.warehouse.id, 5)])

    # Berlin serves only DE/PL now
    def drop_france(s):
        s.shipping_zones[f.zone.id].countries.discard("FR")

    f.shop.context.db.write(drop_france)

    checkout = f.shop.checkouts.create(
        ANONYMOUS, CheckoutCreateInput(channel_id=f.channel.id, shipping_address=PARIS, lines=(line(item, 2),))
    )
    assert f.shop.context.db.read(
        lambda s: [r.warehouse_id for r in s.reservations if r.checkout_id == checkout.id]
    ) == [paris.id]

    f.shop.checkouts.set_shipping_address(checkout.token, BERLIN)

    assert f.shop.context.db.read(
        lambda s: [r.warehouse_id for r in s.reservations if r.checkout_id == checkout.id]
    ) == [f.warehouse.id]


def test_digital_checkout_uses_billing_country(f):
    ebook = f.product("E-book", 9, stock=0, product_type=f.digital_type, digital_url="https://files/book.pdf")
    checkout = f.shop.checkouts.create(
        ANONYMOUS, CheckoutCreateInput(channel_id=f.channel.id, email="x@y.zz", lines=(line(ebook, 3),))
    )
    assert f.shop.checkouts.available_shipping_methods(checkout.token) == []

    # untracked digital goods ignore quantity but need a stock record on a warehouse serving the country
    us = Address(street_address_1="1 Main St", city="Austin", postal_code="73301", country="US", country_area="Texas")
    expect_error(lambda: f.shop.checkouts.set_billing_address(checkout.token, us), ErrorCodes.INSUFFICIENT_STOCK)

    f.shop.checkouts.set_billing_address(checkout.token, WARSAW)
    f.pay_in_full(checkout.token)
    order = f.shop.completion.complete(checkout.token).order
    assert order.delivery is None
    assert (
        f.shop.context.db.read(lambda s: len([a for a in s.allocations if a.order_id == order.id and a.quantity == 3]))
        == 1
    )


def test_untracked_variant_sells_beyond_stock_but_needs_record(f):
    untracked = f.product("Print on demand", 9, stock=0, track_inventory=False)
    no_record = f.product("Ghost", 9, stock=-1, track_inventory=False)

    assert len(f.new_checkout((untracked, 40)).lines) == 1
    expect_error(lambda: f.new_checkout((no_record, 1)), ErrorCodes.INSUFFICIENT_STOCK)


def test_allocation_strategies(f):
    second = f.shop.inventory.create_warehouse(
        f.owner,
        WarehouseCreateInput(name="Munich", address=dataclasses.replace(BERLIN, city="Munich", postal_code="80331")),
    )

    def add_to_zone(s):
        s.shipping_zones[f.zone.id].warehouse_ids.add(second.id)

    f.shop.context.db.write(add_to_zone)
    f.shop.channels.update(
        f.owner, f.channel.id, ChannelUpdateInput(warehouse_ids=(f.warehouse.id, second.id), allow_unpaid_orders=True)
    )
    item = f.product("Item", 1, stock=2)
    f.shop.inventory.update_stocks(f.owner, [StockInput(item.id, second.id, 10)])

    by_priority = f.new_checkout((item, 5))
    f.ready_for_payment(by_priority.token)
    o1 = f.shop.completion.complete(by_priority.token).order

    allocations = f.shop.context.db.read(
        lambda s: {a.warehouse_id: a.quantity for a in s.allocations if a.order_id == o1.id}
    )
    assert allocations[f.warehouse.id] == 2
    assert allocations[second.id] == 3

    f.shop.channels.update(
        f.owner, f.channel.id, ChannelUpdateInput(allocation_strategy=AllocationStrategy.PRIORITIZE_HIGH_STOCK)
    )
    f.shop.inventory.update_stocks(f.owner, [StockInput(item.id, f.warehouse.id, 4)])
    by_stock = f.new_checkout((item, 1))
    f.ready_for_payment(by_stock.token)
    o2 = f.shop.completion.complete(by_stock.token).order
    assert f.shop.context.db.read(lambda s: [a.warehouse_id for a in s.allocations if a.order_id == o2.id]) == [
        second.id
    ]


def test_quantity_limits_are_summed_per_variant_across_lines(f):
    f.settings.max_checkout_line_quantity = 10
    item = f.product("Item", 1, stock=100)
    f.shop.catalog.update_variant(f.owner, item.id, VariantUpdateInput(quantity_limit_per_checkout=3))
    checkout = f.new_checkout()
    f.shop.checkouts.add_lines(ANONYMOUS, checkout.token, [line(item, 2)])
    f.shop.checkouts.add_lines(
        ANONYMOUS, checkout.token, [CheckoutLineInput(variant_id=item.id, quantity=1, metadata={"engraving": "A"})]
    )

    expect_error(
        lambda: f.shop.checkouts.add_lines(
            ANONYMOUS, checkout.token, [CheckoutLineInput(variant_id=item.id, quantity=1, metadata={"engraving": "B"})]
        ),
        ErrorCodes.QUANTITY_LIMIT_EXCEEDED,
    )

    other = f.product("Other", 1, stock=100)
    expect_error(lambda: f.shop.checkouts.add_lines(ANONYMOUS, checkout.token, [line(other, 11)]))
    expect_error(lambda: f.shop.checkouts.add_lines(ANONYMOUS, checkout.token, [line(other, 0)]))


def test_checkout_problems(f):
    item = f.product("Item", 1, stock=5)
    other = f.product("Other", 1, stock=5)
    checkout = f.new_checkout((item, 3), (other, 1))
    f.shop.inventory.update_stocks(f.owner, [StockInput(item.id, f.warehouse.id, 1)])
    f.shop.catalog.update_product_channel_listings(
        f.owner,
        other.product_id,
        [ProductChannelListingInput(channel_id=f.channel.id, is_published=True, is_available_for_purchase=False)],
    )

    view = f.shop.checkouts.set_email(checkout.token, "new@example.com")

    assert any(
        p.type == CheckoutProblemType.INSUFFICIENT_STOCK and p.variant_id == item.id and p.available_quantity == 1
        for p in view.problems
    )
    assert any(p.type == CheckoutProblemType.VARIANT_NOT_AVAILABLE and p.variant_id == other.id for p in view.problems)
    expect_error(lambda: f.shop.completion.complete(checkout.token), ErrorCodes.INSUFFICIENT_STOCK)


def test_preorder_limit_allocates_without_warehouse(f):
    f.shop.channels.update(f.owner, f.channel.id, ChannelUpdateInput(allow_unpaid_orders=True))
    preorder = f.product("Console", 500, stock=-1, preorder=PreorderSettings(10, None))
    checkout = f.new_checkout((preorder, 10))

    expect_error(
        lambda: f.shop.checkouts.add_lines(ANONYMOUS, checkout.token, [line(preorder, 1)]),
        ErrorCodes.INSUFFICIENT_STOCK,
    )

    f.ready_for_payment(checkout.token)
    order = f.shop.completion.complete(checkout.token).order

    def check(s):
        [allocation] = list(s.preorder_allocations)
        assert allocation.order_id == order.id
        assert allocation.channel_id == f.channel.id
        assert s.allocations == []

    f.shop.context.db.read(check)
    expect_error(lambda: f.new_checkout((preorder, 1)))


def test_preorder_channel_threshold_and_reservations(f):
    f.settings.reserve_stock_minutes_anonymous = 10
    preorder = f.product("Book 2", 20, stock=-1, preorder=PreorderSettings(None, None), preorder_channel_threshold=3)
    f.new_checkout((preorder, 2))

    assert (
        f.shop.context.db.read(
            lambda s: len(
                [r for r in s.reservations if r.preorder_channel_id == f.channel.id and r.warehouse_id is None]
            )
        )
        == 1
    )
    expect_error(lambda: f.new_checkout((preorder, 2)))
    assert len(f.new_checkout((preorder, 1)).lines) == 1


def preorder_with_order(f: ShopFixture, quantity: int, stock: int, end=None):
    f.shop.channels.update(f.owner, f.channel.id, ChannelUpdateInput(allow_unpaid_orders=True))
    variant = f.product("Preorder", 10, stock=stock, preorder=PreorderSettings(None, end))
    checkout = f.new_checkout((variant, quantity))
    f.ready_for_payment(checkout.token)
    return variant, f.shop.completion.complete(checkout.token).order.id


def test_manual_preorder_completion(f):
    variant, order_id = preorder_with_order(f, 5, stock=2)

    f.shop.inventory.complete_preorder(f.owner, variant.id)

    def check(s):
        assert len(s.preorder_allocations) == 0
        [allocation] = s.allocations
        assert (allocation.order_id, allocation.warehouse_id, allocation.quantity) == (order_id, f.warehouse.id, 5)
        assert s.stocks[(f.warehouse.id, variant.id)].quantity_allocated == 5  # may exceed stock
        assert s.variants[variant.id].preorder is None

    f.shop.context.db.read(check)


def test_manual_completion_without_eligible_warehouse_fails(f):
    variant, _ = preorder_with_order(f, 5, stock=-1)

    expect_error(lambda: f.shop.inventory.complete_preorder(f.owner, variant.id), ErrorCodes.PREORDER_WAREHOUSE_MISSING)

    def check(s):
        assert s.variants[variant.id].preorder is not None
        assert len(s.preorder_allocations) == 1

    f.shop.context.db.read(check)


def test_ended_preorder(f):
    variant, _ = preorder_with_order(f, 5, stock=7, end=f.clock.utc_now + timedelta(days=1))

    f.clock.advance(timedelta(days=2))  # ended, not migrated yet
    expect_error(lambda: f.new_checkout((variant, 3)), ErrorCodes.INSUFFICIENT_STOCK)
    checkout = f.new_checkout((variant, 2))
    f.shop.checkouts.delete_lines(checkout.token, [line.id for line in checkout.lines])

    assert f.shop.inventory.migrate_ended_preorders() == 1

    def check(s):
        stock = s.stocks[(f.warehouse.id, variant.id)]
        assert stock.quantity_allocated == 5
        assert stock_logic.available(s, stock, f.clock.utc_now) == 2

    f.shop.context.db.read(check)


def test_failed_migration_event_at_most_daily(f):
    app = f.shop.apps.create_local_app(f.owner, "ops", []).app
    f.shop.apps.subscribe(
        f.owner,
        app.id,
        "https://ops.example.com/hook",
        [EventTypes.PREORDER_MIGRATION_FAILED],
        "{ productVariant { id } }",
    )
    preorder_with_order(f, 5, stock=-1, end=f.clock.utc_now + timedelta(minutes=1))

    def events():
        f.run_jobs()
        return sum(1 for r in f.webhooks.sent if r.headers["X-Event-Type"] == EventTypes.PREORDER_MIGRATION_FAILED)

    f.clock.advance(timedelta(minutes=2))
    f.shop.inventory.migrate_ended_preorders()
    assert events() == 1

    f.clock.advance(timedelta(hours=1))
    f.shop.inventory.migrate_ended_preorders()
    assert events() == 1

    f.clock.advance(timedelta(days=1))
    f.shop.inventory.migrate_ended_preorders()
    assert events() == 2


def test_stock_events_out_of_stock_back_in_stock_low_stock(f):
    app = f.shop.apps.create_local_app(f.owner, "stock", []).app
    f.shop.apps.subscribe(
        f.owner,
        app.id,
        "https://stock.example.com",
        [
            EventTypes.PRODUCT_VARIANT_OUT_OF_STOCK,
            EventTypes.PRODUCT_VARIANT_BACK_IN_STOCK,
            EventTypes.PRODUCT_VARIANT_LOW_STOCK,
        ],
        "{ productVariant { sku } }",
    )
    item = f.product("Item", 1, stock=5, sku="EVT-1")

    f.shop.inventory.update_stocks(f.owner, [StockInput(item.id, f.warehouse.id, 0)])
    f.shop.inventory.update_stocks(f.owner, [StockInput(item.id, f.warehouse.id, 2, low_stock_threshold=3)])
    f.run_jobs()

    types = [r.headers["X-Event-Type"] for r in f.webhooks.sent]
    assert types == [
        EventTypes.PRODUCT_VARIANT_OUT_OF_STOCK,
        EventTypes.PRODUCT_VARIANT_BACK_IN_STOCK,
        EventTypes.PRODUCT_VARIANT_LOW_STOCK,
    ]
    assert all(r.body == '{"productVariant":{"sku":"EVT-1"}}' for r in f.webhooks.sent)


def test_rest_of_world_zone(f):
    channel_b = f.shop.channels.create(f.owner, ChannelCreateInput(name="B", currency="EUR", default_country="DE"))
    rest_of_world = f.shop.shipping.create_zone(
        f.owner,
        ShippingZoneInput(name="Rest", is_default=True, warehouse_ids=(f.warehouse.id,), channel_ids=(channel_b.id,)),
    )

    def check(s):
        assert not stock_logic.zone_covers_country(s, rest_of_world, "DE")
        assert stock_logic.zone_covers_country(s, rest_of_world, "JP")
        assert not stock_logic.warehouse_serves_country(s, s.channels[channel_b.id], f.warehouse.id, "DE")

    f.shop.context.db.read(check)
    expect_error(lambda: f.shop.shipping.create_zone(f.owner, ShippingZoneInput(name="Second rest", is_default=True)))


def test_shipping_method_availability_rules(f):
    heavy = f.shop.shipping.create_method(
        f.owner,
        ShippingMethodInput(
            zone_id=f.zone.id,
            name="Freight",
            type=ShippingMethodType.WEIGHT,
            minimum_weight_kg=D(5),
            channel_listings=(ShippingMethodChannelListing(f.channel.id, D(50)),),
        ),
    )
    berlin_only = f.shop.shipping.create_method(
        f.owner,
        ShippingMethodInput(
            zone_id=f.zone.id,
            name="Courier",
            postal_code_mode=PostalCodeRuleMode.INCLUDE,
            postal_code_ranges=(PostalCodeRange("10000", "14999"),),
            channel_listings=(ShippingMethodChannelListing(f.channel.id, D(5)),),
        ),
    )
    no_shirts = f.shop.shipping.create_method(
        f.owner,
        ShippingMethodInput(
            zone_id=f.zone.id, name="Letter", channel_listings=(ShippingMethodChannelListing(f.channel.id, D(1)),)
        ),
    )
    no_price = f.shop.shipping.create_method(f.owner, ShippingMethodInput(zone_id=f.zone.id, name="Hidden"))
    shirt = f.product("Shirt", 20, product_type=f.shirt_type)

    def exclude(s):
        s.find_shipping_method(no_shirts.id).excluded_product_ids.add(shirt.product_id)

    f.shop.context.db.write(exclude)

    checkout = f.new_checkout((shirt, 2))  # 0.6 kg from product type default weight
    f.shop.checkouts.set_shipping_address(checkout.token, BERLIN)
    ids = [m.id for m in f.shop.checkouts.available_shipping_methods(checkout.token)]
    assert berlin_only.id in ids
    assert f.standard_shipping.id in ids
    assert heavy.id not in ids
    assert no_shirts.id not in ids
    assert no_price.id not in ids

    f.shop.checkouts.set_shipping_address(checkout.token, WARSAW)
    assert all(m.id != berlin_only.id for m in f.shop.checkouts.available_shipping_methods(checkout.token))
    expect_error(
        lambda: f.shop.checkouts.set_delivery(checkout.token, DeliveryInput(shipping_method_id=berlin_only.id))
    )


def test_local_collection_point(f):
    store = f.shop.inventory.create_warehouse(
        f.owner, WarehouseCreateInput(name="Store", address=BERLIN, click_and_collect=ClickAndCollectOption.LOCAL)
    )
    f.shop.channels.update(f.owner, f.channel.id, ChannelUpdateInput(warehouse_ids=(f.warehouse.id, store.id)))
    item = f.product("Item", 1, stock=5)
    checkout = f.new_checkout((item, 2))
    assert f.shop.checkouts.available_collection_points(checkout.token) == []

    f.shop.inventory.update_stocks(f.owner, [StockInput(item.id, store.id, 2)])
    assert len(f.shop.checkouts.available_collection_points(checkout.token)) == 1
