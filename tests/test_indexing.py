from concurrent.futures import ThreadPoolExecutor

from sales_api.accounts.model import User
from sales_api.catalog.queries import ProductQuery, ProductSortField
from sales_api.catalog.service import ProductCreateInput, ProductUpdateInput, VariantCreateInput, VariantUpdateInput
from sales_api.common.errors import ErrorCodes
from sales_api.common.pagination import SortDirection
from sales_api.discounts.model import DiscountValueType, VoucherChannelListing, VoucherType
from sales_api.discounts.service import VoucherInput
from sales_api.permissions.model import ANONYMOUS
from support import D, ShopFixture, expect_error


def search(f: ShopFixture, q: str):
    return f.shop.catalog_queries.products(
        ANONYMOUS, f.channel.id, ProductQuery(search=q, sort=ProductSortField.RANK)
    ).data.items


def test_product_slug_change_is_found_by_new_slug_only(f):
    variant = f.product("Old Name", 10)
    product = f.shop.context.db.read(lambda s: s.products[variant.product_id])
    old_slug = product.slug

    f.shop.catalog.update_product(f.owner, product.id, ProductUpdateInput(slug="brand-new"))

    assert f.shop.catalog_queries.product(f.owner, f.channel.id, old_slug).data is None
    assert f.shop.catalog_queries.product(f.owner, f.channel.id, "brand-new").data.id == product.id
    other = f.shop.catalog.create_product(
        f.owner, ProductCreateInput(product_type_id=f.simple_type.id, name="Other", slug=old_slug)
    )
    assert other.slug == old_slug


def test_user_email_change_is_found_by_new_email_only(f):
    def check(store):
        user = User(email="a@example.com", created_at=f.clock.utc_now)
        store.users.add(user.id, user)
        store.users.set_email(user, "B@Example.com ")

        assert store.find_user_by_email("a@example.com") is None
        assert store.find_user_by_email("b@example.com") is user
        store.users.remove(user.id)
        assert store.find_user_by_email("b@example.com") is None

    f.shop.context.db.write(check)


def test_variant_sku_change_frees_old_sku_and_reserves_new_one(f):
    variant = f.product("Shirt", 10, sku="OLD-1")
    f.shop.catalog.update_variant(f.owner, variant.id, VariantUpdateInput(sku="NEW-1"))

    product = f.shop.catalog.create_product(
        f.owner, ProductCreateInput(product_type_id=f.simple_type.id, name="Second")
    )
    expect_error(
        lambda: f.shop.catalog.create_variant(f.owner, VariantCreateInput(product_id=product.id, sku="new-1")),
        ErrorCodes.DUPLICATE,
    )
    assert f.shop.catalog.create_variant(f.owner, VariantCreateInput(product_id=product.id, sku="OLD-1")).sku == "OLD-1"


def test_deleted_product_leaves_no_variants_or_skus_behind(f):
    variant = f.product("Gone", 10, sku="GONE-1")
    f.shop.catalog.delete_product(f.owner, variant.product_id)

    def check(s):
        assert s.variants.of_product(variant.product_id) == []
        assert s.variants.find_by_sku("GONE-1") is None
        assert s.stocks_of_variant(variant.id) == []

    f.shop.context.db.read(check)


def test_generated_voucher_codes_are_redeemable(f):
    voucher = f.shop.discounts.create_voucher(
        f.owner,
        VoucherInput(
            name="Bulk",
            type=VoucherType.ENTIRE_ORDER,
            codes=("FIRST",),
            value_type=DiscountValueType.FIXED,
            channel_listings=(VoucherChannelListing(f.channel.id, D(1)),),
        ),
    )
    codes = f.shop.discounts.generate_codes(f.owner, voucher.id, 50, "promo")

    assert len(set(codes)) == 50

    def check(s):
        assert all(s.find_voucher_by_code(c.lower())[0] is voucher for c in codes)
        found, code = s.find_voucher_by_code(" first ")
        assert found is voucher
        assert code.code == "FIRST"

    f.shop.context.db.read(check)

    checkout = f.new_checkout((f.product("Item", 10), 1))
    assert f.shop.checkouts.apply_voucher(checkout.token, codes[0]).checkout.voucher_code == codes[0]


def test_deleted_variant_drops_its_reservations(f):
    f.settings.reserve_stock_minutes_anonymous = 10
    variant = f.product("Reserved", 10, stock=5)
    f.new_checkout((variant, 2))
    assert f.shop.context.db.read(lambda s: len(s.reservations.for_stock(f.warehouse.id, variant.id))) == 1

    f.shop.catalog.delete_variant(f.owner, variant.id)

    def check(s):
        assert s.reservations.for_stock(f.warehouse.id, variant.id) == []
        assert all(r.variant_id != variant.id for r in s.reservations)

    f.shop.context.db.read(check)


def test_search_reindex_drops_stale_tokens(f):
    variant = f.product("Ultraboost Runner", 10, sku="RUN-7")
    f.run_jobs()
    assert len(search(f, "ultra run")) == 1

    f.shop.catalog.update_product(f.owner, variant.product_id, ProductUpdateInput(name="Classic Walker"))
    f.shop.catalog.update_variant(f.owner, variant.id, VariantUpdateInput(name="Classic Walker"))
    f.run_jobs()

    assert search(f, "ultra") == []
    assert len(search(f, "class walk")) == 1
    assert len(search(f, "run")) == 1  # still matched through the SKU
    assert search(f, "class ultra") == []

    f.shop.catalog.delete_product(f.owner, variant.product_id)
    assert search(f, "class") == []


def test_search_ranks_by_best_weight_per_term(f):
    f.product("Red Lamp", 10, sku="X-1")
    f.product("Lamp", 10, sku="RED-2")  # "red" only via the SKU (lower weight than the name)
    f.run_jobs()

    assert [p.name for p in search(f, "red lamp")] == ["Red Lamp", "Lamp"]


def test_pagination_walks_all_pages_without_gaps_or_duplicates(f):
    for i in range(7):
        f.product(f"P{i}", 10 + i)
    seen = []
    after = None
    pages = 0
    while True:
        page = f.shop.catalog_queries.products(
            ANONYMOUS,
            f.channel.id,
            ProductQuery(first=3, after=after, sort=ProductSortField.PRICE, direction=SortDirection.DESC),
        ).data
        assert page.total_count == 7
        seen.extend(p.id for p in page.items)
        after = page.end_cursor if page.has_next_page else None
        pages += 1
        if after is None:
            break

    assert pages == 3
    assert len(set(seen)) == 7
    prices = [
        f.shop.context.db.read(
            lambda s, pid=product_id: s.variants.of_product(pid)[0].channel_listings[f.channel.id].price
        )
        for product_id in seen
    ]
    assert prices == sorted(prices, reverse=True)


def test_parallel_reads_and_writes_do_not_interfere(f):
    f.product("Seed", 10)

    def work(i):
        if i % 4 == 0:
            f.product(f"Parallel {i}", 10 + i)
        else:
            f.shop.catalog_queries.products(f.owner, f.channel.id, ProductQuery(first=100, in_stock=True))

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(work, range(40)))  # re-raises the first exception, if any

    assert f.shop.catalog_queries.products(f.owner, f.channel.id, ProductQuery(first=100)).data.total_count == 11
