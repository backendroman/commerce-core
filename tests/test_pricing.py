from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from sales_api.catalog.service import ProductCreateInput, ProductTypeCreateInput, VariantCreateInput, VariantPriceInput
from sales_api.checkout.service import CheckoutCreateInput, CheckoutLineInput
from sales_api.common.errors import DomainException, ErrorCodes
from sales_api.discounts.model import (
    ComparisonOperator,
    DiscountValueType,
    PromotionType,
    SubtotalCondition,
    VoucherChannelListing,
    VoucherType,
)
from sales_api.discounts.rules import is_valid_code_format
from sales_api.discounts.service import GiftCardInput, PromotionInput, PromotionRuleInput, VoucherInput
from sales_api.permissions import resolver
from sales_api.permissions.model import ANONYMOUS, Permission
from sales_api.shipping.model import ShippingMethodChannelListing
from sales_api.shipping.service import ShippingMethodInput
from sales_api.taxes.model import TaxAppFailureBehavior, TaxCountryException, TaxStrategy
from sales_api.taxes.service import TaxConfigurationInput
from support import BERLIN, D, FakeTaxApp, ShopFixture, expect_error, line, published


def order_gift_promotion(f: ShopFixture, threshold, *gifts):
    return f.shop.discounts.create_promotion(
        f.owner,
        PromotionInput(
            name="Gift over threshold",
            type=PromotionType.ORDER,
            rules=(
                PromotionRuleInput(
                    channel_ids=(f.channel.id,),
                    condition=SubtotalCondition(ComparisonOperator.GT, D(threshold)),
                    gift_variant_ids=tuple(gifts),
                ),
            ),
        ),
    )


def order_discount_promotion(f: ShopFixture, threshold, value_type: DiscountValueType, value):
    f.shop.discounts.create_promotion(
        f.owner,
        PromotionInput(
            name="Order discount",
            type=PromotionType.ORDER,
            rules=(
                PromotionRuleInput(
                    channel_ids=(f.channel.id,),
                    condition=SubtotalCondition(ComparisonOperator.GTE, D(threshold)),
                    value_type=value_type,
                    value=D(value),
                ),
            ),
        ),
    )


def voucher(f: ShopFixture, name: str, code: str, value, **kwargs):
    listing = VoucherChannelListing(f.channel.id, D(value), kwargs.pop("min_spent", None))
    return f.shop.discounts.create_voucher(
        f.owner,
        VoucherInput(
            name=name,
            type=kwargs.pop("type", VoucherType.ENTIRE_ORDER),
            codes=(code,),
            channel_listings=(listing,),
            **kwargs,
        ),
    )


def catalogue_promotion(f: ShopFixture, name: str, **rule):
    return f.shop.discounts.create_promotion(
        f.owner,
        PromotionInput(
            name=name,
            type=PromotionType.CATALOGUE,
            starts_at=rule.pop("starts_at", None),
            ends_at=rule.pop("ends_at", None),
            rules=(PromotionRuleInput(channel_ids=(f.channel.id,), **rule),),
        ),
    )


def test_gift_promotion_lifecycle(f):
    expensive = f.product("Watch", 100)
    cheap_gift = f.product("Sticker", 2)
    rich_gift = f.product("Tote", 8)
    out_of_stock_gift = f.product("Rare", 50, stock=0)
    small = f.product("Pin", 5)
    order_gift_promotion(f, 90, cheap_gift.id, out_of_stock_gift.id, rich_gift.id)

    checkout = f.new_checkout((expensive, 1), (small, 1))
    view = f.shop.checkouts.get(checkout.token)

    # most expensive available gift by default; out-of-stock ones are not offered
    [gift] = [line for line in view.checkout.lines if line.is_gift]
    assert gift.variant_id == rich_gift.id
    assert next(line for line in view.pricing.lines if line.is_gift).total.gross.amount == 0
    assert view.pricing.subtotal.gross.amount == 105

    view = f.shop.checkouts.select_gift(checkout.token, cheap_gift.id)
    assert next(line for line in view.checkout.lines if line.is_gift).variant_id == cheap_gift.id
    expect_error(lambda: f.shop.checkouts.select_gift(checkout.token, out_of_stock_gift.id))

    watch_line = next(line for line in view.checkout.lines if line.variant_id == expensive.id)
    view = f.shop.checkouts.delete_lines(checkout.token, [watch_line.id])
    assert not any(line.is_gift for line in view.checkout.lines)


def test_gift_is_reserved_and_allocated_like_regular_line(f):
    f.settings.reserve_stock_minutes_anonymous = 10
    item = f.product("TV", 500)
    gift = f.product("Cable", 10, stock=1)
    order_gift_promotion(f, 100, gift.id)
    checkout = f.new_checkout((item, 1))
    assert f.shop.context.db.read(
        lambda s: any(r.variant_id == gift.id and r.checkout_id == checkout.id for r in s.reservations)
    )

    f.ready_for_payment(checkout.token)
    f.pay_in_full(checkout.token)
    order = f.shop.completion.complete(checkout.token).order

    assert any(line.is_gift and line.variant_id == gift.id and line.total.gross.amount == 0 for line in order.lines)
    assert f.shop.context.db.read(lambda s: s.stocks[(f.warehouse.id, gift.id)].quantity_allocated) == 1


def test_entire_order_voucher_disables_order_promotion(f):
    item = f.product("Bike", 200)
    gift = f.product("Bell", 5)
    order_gift_promotion(f, 100, gift.id)
    voucher(f, "Whole order", "WHOLE", 10)
    checkout = f.new_checkout((item, 1))
    assert any(line.is_gift for line in f.shop.checkouts.get(checkout.token).checkout.lines)

    view = f.shop.checkouts.apply_voucher(checkout.token, "WHOLE")

    assert not any(line.is_gift for line in view.checkout.lines)
    assert view.pricing.order_reward is None
    assert view.pricing.subtotal.gross.amount == 180


def test_product_voucher_with_order_promo(f):
    shoes = f.product("Shoes", 100)
    socks = f.product("Socks", 10)
    voucher(f, "Shoes -50%", "SHOES50", 50, type=VoucherType.SPECIFIC_PRODUCT, product_ids=(shoes.product_id,))
    order_discount_promotion(f, 100, DiscountValueType.PERCENTAGE, 10)
    checkout = f.new_checkout((shoes, 1), (socks, 1))

    view = f.shop.checkouts.apply_voucher(checkout.token, "SHOES50")

    assert view.pricing.order_reward is not None
    assert view.pricing.subtotal.gross.amount == 54


def test_best_order_rule_wins_gift_valued_at_discounted_price(f):
    item = f.product("Camera", 300)
    gift = f.product("Strap", 40)
    catalogue_promotion(f, "Strap -50%", variant_ids=(gift.id,), value_type=DiscountValueType.PERCENTAGE, value=D(50))
    order_gift_promotion(f, 100, gift.id)  # gift worth 20 after catalogue discount
    order_discount_promotion(f, 100, DiscountValueType.FIXED, 25)

    view = f.shop.checkouts.get(f.new_checkout((item, 1)).token)

    assert not view.pricing.order_reward.rule.is_gift_reward
    assert view.pricing.subtotal.gross.amount == 275
    assert not any(line.is_gift for line in view.checkout.lines)


def test_catalogue_discount_rounds_on_unit_price_best_rule_wins(f):
    item = f.product("Tea", "3.33")
    f.shop.discounts.create_promotion(
        f.owner,
        PromotionInput(
            name="Two rules",
            type=PromotionType.CATALOGUE,
            rules=(
                PromotionRuleInput(
                    channel_ids=(f.channel.id,),
                    category_ids=(f.category.id,),
                    value_type=DiscountValueType.PERCENTAGE,
                    value=D(15),
                ),
                PromotionRuleInput(
                    channel_ids=(f.channel.id,),
                    variant_ids=(item.id,),
                    value_type=DiscountValueType.FIXED,
                    value=D("0.10"),
                ),
            ),
        ),
    )

    view = f.shop.checkouts.get(f.new_checkout((item, 3)).token)

    [line] = view.pricing.lines
    assert line.catalogue_unit_price == D("2.83")
    assert line.total.gross.amount == D("8.49")


def test_order_discount_distribution(f):
    a = f.product("A", 10)
    b = f.product("B", 10)
    c = f.product("C", 10)
    voucher(f, "Ten", "TENOFF", 10, value_type=DiscountValueType.FIXED)
    checkout = f.new_checkout((a, 1), (b, 1), (c, 1))

    lines = f.shop.checkouts.apply_voucher(checkout.token, "TENOFF").pricing.lines

    assert [line.total.gross.amount for line in lines] == [D("6.67"), D("6.67"), D("6.66")]


def test_discounts_never_make_amounts_negative(f):
    item = f.product("Gum", 1)
    voucher(f, "Huge", "HUGE", 100, value_type=DiscountValueType.FIXED)
    voucher(f, "Ship", "FREESHIP", 100, type=VoucherType.SHIPPING, value_type=DiscountValueType.FIXED)
    checkout = f.new_checkout((item, 1))
    f.ready_for_payment(checkout.token)

    assert f.shop.checkouts.apply_voucher(checkout.token, "HUGE").pricing.total.gross.amount == 10
    assert f.shop.checkouts.apply_voucher(checkout.token, "FREESHIP").pricing.total.gross.amount == 1


def test_voucher_once_per_order(f):
    a = f.product("A", 30)
    b = f.product("B", 20)
    voucher(
        f,
        "Cheapest",
        "CHEAP",
        50,
        type=VoucherType.SPECIFIC_PRODUCT,
        category_ids=(f.category.id,),
        apply_once_per_order=True,
    )
    checkout = f.new_checkout((a, 2), (b, 2))

    pricing = f.shop.checkouts.apply_voucher(checkout.token, "CHEAP").pricing

    assert next(line for line in pricing.lines if line.variant_id == a.id).total.gross.amount == 60
    assert next(line for line in pricing.lines if line.variant_id == b.id).total.gross.amount == 30


def test_voucher_restrictions(f):
    item = f.product("Item", 10)
    voucher(f, "Min", "MIN50", 10, min_spent=D(50))
    voucher(f, "Qty", "QTY3", 10, min_checkout_items_quantity=3)
    voucher(f, "Once", "ONCE", 10, apply_once_per_customer=True)
    voucher(f, "Future", "LATER", 10, starts_at=f.clock.utc_now + timedelta(days=1))
    checkout = f.new_checkout((item, 1))

    expect_error(lambda: f.shop.checkouts.apply_voucher(checkout.token, "MIN50"), ErrorCodes.CODE_REJECTED)
    expect_error(lambda: f.shop.checkouts.apply_voucher(checkout.token, "QTY3"))
    expect_error(lambda: f.shop.checkouts.apply_voucher(checkout.token, "LATER"))
    expect_error(lambda: f.shop.checkouts.apply_voucher(checkout.token, "UNKNOWN"))

    # once per customer = per checkout email
    f.ready_for_payment(checkout.token)
    f.shop.checkouts.apply_voucher(checkout.token, "once")
    f.pay_in_full(checkout.token)
    f.shop.completion.complete(checkout.token)
    again = f.new_checkout((item, 1))
    expect_error(lambda: f.shop.checkouts.apply_voucher(again.token, "ONCE"))

    no_email = f.shop.checkouts.create(ANONYMOUS, CheckoutCreateInput(channel_id=f.channel.id, lines=(line(item, 1),)))
    expect_error(lambda: f.shop.checkouts.apply_voucher(no_email.token, "ONCE"))


def test_only_one_voucher_per_checkout_new_one_replaces_old(f):
    item = f.product("Item", 100)
    voucher(f, "A", "CODEA", 10)
    voucher(f, "B", "CODEB", 20)
    checkout = f.new_checkout((item, 1))
    f.shop.checkouts.apply_voucher(checkout.token, "CODEA")

    view = f.shop.checkouts.apply_voucher(checkout.token, "CODEB")

    assert view.checkout.voucher_code == "CODEB"
    assert view.pricing.subtotal.gross.amount == 80


def test_single_use_voucher_last_use_taken_by_exactly_one_order(f):
    item = f.product("Item", 10, stock=100)
    voucher(f, "Limited", "LAST1", 10, usage_limit=1)
    app = f.install_payment_app()
    tokens = []
    for _ in range(2):
        c = f.new_checkout((item, 1))
        f.ready_for_payment(c.token)
        f.shop.checkouts.apply_voucher(c.token, "LAST1")
        f.pay_in_full(c.token, app)
        tokens.append(c.token)

    def complete(token):
        try:
            f.shop.completion.complete(token)
            return None
        except DomainException as ex:
            return ex.errors[0].code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(complete, tokens))

    assert results.count(None) == 1
    assert results.count(ErrorCodes.CODE_REJECTED) == 1


def test_staff_only_voucher(f):
    staff_user = f.shop.accounts.register("anna@example.com", "Password123")

    def promote(s):
        s.users[staff_user.id].is_staff = True
        s.users[staff_user.id].is_active = True

    f.shop.context.db.write(promote)
    item = f.product("Item", 10)
    voucher(f, "Staff", "STAFF", 30, only_for_staff=True)
    checkout = f.shop.checkouts.create(
        ANONYMOUS, CheckoutCreateInput(channel_id=f.channel.id, email="anna@example.com", lines=(line(item, 1),))
    )

    expect_error(lambda: f.shop.checkouts.apply_voucher(checkout.token, "STAFF"))

    f.shop.checkouts.attach_customer(f.principal_of(staff_user.id), checkout.token)
    assert f.shop.checkouts.apply_voucher(checkout.token, "STAFF").pricing.subtotal.gross.amount == 7


def test_thresholds_use_subtotal_after_catalogue_discount(f):
    item = f.product("Item", 100)
    catalogue_promotion(f, "-20%", variant_ids=(item.id,), value_type=DiscountValueType.PERCENTAGE, value=D(20))
    voucher(f, "Min 90", "MIN90", 5, min_spent=D(90))
    expensive = f.shop.shipping.create_method(
        f.owner,
        ShippingMethodInput(
            zone_id=f.zone.id,
            name="Free over 90",
            channel_listings=(ShippingMethodChannelListing(f.channel.id, D(0), minimum_order_price=D(90)),),
        ),
    )
    checkout = f.new_checkout((item, 1))
    f.shop.checkouts.set_shipping_address(checkout.token, BERLIN)

    expect_error(lambda: f.shop.checkouts.apply_voucher(checkout.token, "MIN90"))
    assert all(m.id != expensive.id for m in f.shop.checkouts.available_shipping_methods(checkout.token))


def test_gift_card_rules(f):
    item = f.product("Item", 20)
    voucher_product_type = f.shop.catalog.create_product_type(
        f.owner, ProductTypeCreateInput(name="Gift card product", is_gift_card=True, is_shipping_required=False)
    )
    gift_card_product = f.product("E-gift", 30, product_type=voucher_product_type)
    expired = f.shop.discounts.issue_gift_card(
        f.owner, GiftCardInput(balance=D(10), currency="EUR", expiry_date=f.clock.utc_now.date())
    )
    usd = f.shop.discounts.issue_gift_card(f.owner, GiftCardInput(balance=D(10), currency="USD"))
    inactive = f.shop.discounts.issue_gift_card(f.owner, GiftCardInput(balance=D(10), currency="EUR", is_active=False))
    first = f.shop.discounts.issue_gift_card(f.owner, GiftCardInput(balance=D(15), currency="EUR"))
    second = f.shop.discounts.issue_gift_card(f.owner, GiftCardInput(balance=D(100), currency="EUR"))
    checkout = f.new_checkout((item, 1), (gift_card_product, 1))
    f.ready_for_payment(checkout.token)

    # valid until the end of the day (UTC)
    f.shop.checkouts.add_gift_card(checkout.token, expired.code)
    f.shop.checkouts.remove_gift_card(checkout.token, expired.id)
    f.clock.advance(timedelta(days=1))
    expect_error(lambda: f.shop.checkouts.add_gift_card(checkout.token, expired.code))
    expect_error(lambda: f.shop.checkouts.add_gift_card(checkout.token, usd.code))
    expect_error(lambda: f.shop.checkouts.add_gift_card(checkout.token, inactive.code))

    f.shop.checkouts.add_gift_card(checkout.token, first.code)
    pricing = f.shop.checkouts.add_gift_card(checkout.token, second.code).pricing

    assert pricing.total.gross.amount == 60
    assert [g.amount for g in pricing.gift_cards] == [15, 15]
    assert pricing.amount_to_pay == 30


def test_net_prices_get_tax_on_top_per_line_rounding(f):
    f.shop.taxes.configure(f.owner, f.channel.id, TaxConfigurationInput(prices_entered_with_tax=False))
    reduced = f.shop.taxes.create_tax_class(f.owner, "Books", {"DE": D(7)})
    f.shop.taxes.set_country_default_rate(f.owner, "DE", D(19))
    book = f.shop.catalog.create_product(
        f.owner,
        ProductCreateInput(
            product_type_id=f.simple_type.id, name="Novel", category_id=f.category.id, tax_class_id=reduced.id
        ),
    )
    f.shop.catalog.update_product_channel_listings(f.owner, book.id, [published(f.channel.id)])
    book_variant = f.shop.catalog.create_variant(
        f.owner,
        VariantCreateInput(
            product_id=book.id,
            channel_listings=(VariantPriceInput(channel_id=f.channel.id, price=D("9.99")),),
            stocks=((f.warehouse.id, 10),),
        ),
    )
    pen = f.product("Pen", "1.05")
    checkout = f.new_checkout((book_variant, 3), (pen, 1))
    pricing = f.ready_for_payment(checkout.token).pricing

    book_line = next(line for line in pricing.lines if line.variant_id == book_variant.id)
    assert book_line.total.net.amount == D("29.97")
    assert book_line.total.gross.amount == D("32.07")
    assert next(line for line in pricing.lines if line.variant_id == pen.id).total.gross.amount == D("1.25")
    assert pricing.shipping.gross.amount == D("11.90")
    assert pricing.total.gross.amount == D("45.22")


def test_gross_prices_without_tax(f):
    f.shop.taxes.set_country_default_rate(f.owner, "DE", D(19))
    item = f.product("Item", 119)
    checkout = f.new_checkout((item, 1))
    assert f.shop.checkouts.get(checkout.token).pricing.subtotal.net.amount == 100

    exempt = f.shop.checkouts.set_tax_exemption(f.owner, checkout.token, True).pricing
    assert exempt.subtotal.gross.amount == 119
    assert exempt.subtotal.tax.amount == 0

    expect_error(lambda: f.shop.checkouts.set_tax_exemption(ANONYMOUS, checkout.token, False))
    f.shop.checkouts.set_tax_exemption(f.owner, checkout.token, False)
    f.shop.taxes.configure(
        f.owner,
        f.channel.id,
        TaxConfigurationInput(
            country_exceptions={
                "DE": TaxCountryException(
                    charge_taxes=False, display_gross_prices=True, strategy=TaxStrategy.FLAT_RATES
                )
            }
        ),
    )
    not_charged = f.shop.checkouts.get_pricing(checkout.token, force_recalculate=True)
    assert not_charged.subtotal.gross.amount == 119
    assert not_charged.subtotal.tax.amount == 0


def test_external_tax_app_with_timeout_fallback(f):
    app = f.shop.apps.create_local_app(f.owner, "Avalara-like", [Permission.MANAGE_TAXES]).app
    tax_app = FakeTaxApp(D(10))
    f.shop.context.app_handlers.register(app.id, tax_app)
    f.shop.taxes.configure(
        f.owner,
        f.channel.id,
        TaxConfigurationInput(strategy=TaxStrategy.TAX_APP, tax_app_id=app.id, prices_entered_with_tax=False),
    )
    f.shop.taxes.set_country_default_rate(f.owner, "DE", D(19))
    item = f.product("Item", 100)
    checkout = f.new_checkout((item, 1))

    assert f.shop.checkouts.get(checkout.token).pricing.subtotal.gross.amount == 110

    # app too slow (timeout is 2 s in the fixture): error by default
    tax_app.delay = timedelta(seconds=3)
    expect_error(
        lambda: f.shop.checkouts.get_pricing(checkout.token, force_recalculate=True), ErrorCodes.TAX_CALCULATION_FAILED
    )

    # ...or built-in rates when configured so
    f.shop.taxes.configure(
        f.owner, f.channel.id, TaxConfigurationInput(app_failure_behavior=TaxAppFailureBehavior.USE_FLAT_RATES)
    )
    assert f.shop.checkouts.get_pricing(checkout.token, force_recalculate=True).subtotal.gross.amount == 119


def test_catalog_prices_use_built_in_rates_even_with_tax_app(f):
    app = f.shop.apps.create_local_app(f.owner, "tax", []).app
    tax_app = FakeTaxApp()
    f.shop.context.app_handlers.register(app.id, tax_app)
    f.shop.taxes.configure(
        f.owner,
        f.channel.id,
        TaxConfigurationInput(strategy=TaxStrategy.TAX_APP, tax_app_id=app.id, prices_entered_with_tax=False),
    )
    item = f.product("Item", 100)
    f.run_jobs()

    slug = f.shop.context.db.read(lambda s: s.products[item.product_id].slug)
    no_rates = f.shop.catalog_queries.product(ANONYMOUS, f.channel.id, slug).data
    assert no_rates.price_range.start.gross.amount == 100

    f.shop.taxes.set_country_default_rate(f.owner, "FR", D(20))
    fr = f.shop.catalog_queries.product(ANONYMOUS, f.channel.id, no_rates.slug, country="FR").data
    assert fr.price_range.start.gross.amount == 120
    assert tax_app.calls == 0


def test_app_line_price(f):
    item = f.product("Item", 50)
    catalogue_promotion(f, "-50%", variant_ids=(item.id,), value_type=DiscountValueType.PERCENTAGE, value=D(50))
    app = f.shop.apps.create_local_app(f.owner, "pos", [Permission.HANDLE_CHECKOUTS]).app
    checkout = f.new_checkout()

    expect_error(
        lambda: f.shop.checkouts.add_lines(
            ANONYMOUS, checkout.token, [CheckoutLineInput(variant_id=item.id, quantity=1, price_override=D(42))]
        )
    )
    view = f.shop.checkouts.add_lines(
        resolver.for_app(app),
        checkout.token,
        [CheckoutLineInput(variant_id=item.id, quantity=1, price_override=D(42)), line(item, 1)],
    )

    assert len(view.checkout.lines) == 2  # a different price makes a separate line
    assert view.pricing.subtotal.gross.amount == 67


def test_price_cache_is_invalidated_by_changes_and_on_request(f):
    item = f.product("Item", 10)
    checkout = f.new_checkout((item, 1))
    first = f.shop.checkouts.get_pricing(checkout.token)

    f.shop.catalog.update_variant_prices(f.owner, item.id, [VariantPriceInput(channel_id=f.channel.id, price=D(12))])
    assert f.shop.checkouts.get_pricing(checkout.token) is first
    assert f.shop.checkouts.get_pricing(checkout.token, force_recalculate=True).subtotal.gross.amount == 12

    f.shop.catalog.update_variant_prices(f.owner, item.id, [VariantPriceInput(channel_id=f.channel.id, price=D(13))])
    f.clock.advance(timedelta(minutes=61))
    assert f.shop.checkouts.get_pricing(checkout.token).subtotal.gross.amount == 13


def test_promotion_scheduler_publishes_start_and_end(f):
    p = catalogue_promotion(
        f,
        "Weekend",
        starts_at=f.clock.utc_now + timedelta(minutes=5),
        ends_at=f.clock.utc_now + timedelta(minutes=10),
        category_ids=(f.category.id,),
        value_type=DiscountValueType.PERCENTAGE,
        value=D(10),
    )

    f.clock.advance(timedelta(minutes=6))
    f.shop.discounts.process_promotion_schedule()
    f.clock.advance(timedelta(minutes=6))
    f.shop.discounts.process_promotion_schedule()

    assert f.shop.context.db.read(lambda s: [item.type for item in s.promotions[p.id].log]) == [
        "CREATED",
        "STARTED",
        "ENDED",
    ]


@pytest.mark.parametrize("code", ["ab", "has space", "ÜMLAUT"])
def test_voucher_code_format_is_enforced(f, code):
    expect_error(lambda: voucher(f, "X", code, 1))


def test_generated_codes_are_unique_and_prefixed(f):
    v = voucher(f, "Batch", "SEED", 1)

    codes = f.shop.discounts.generate_codes(f.owner, v.id, 50, "promo")

    assert len(set(codes)) == 50
    assert all(c.startswith("PROMO-") for c in codes)
    assert all(is_valid_code_format(c) for c in codes)
