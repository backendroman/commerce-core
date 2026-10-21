import json
from datetime import timedelta

import pytest

from sales_api.catalog.attribute_assignment import AttributeValueInput
from sales_api.catalog.indexer import thumbnail_size
from sales_api.catalog.model import AttributeInputType, AttributeScope, MeasurementUnit, ReferenceTarget
from sales_api.catalog.queries import ProductQuery, ProductSortField
from sales_api.catalog.service import (
    AttributeCreateInput,
    CategoryCreateInput,
    ProductChannelListingInput,
    ProductCreateInput,
    ProductTypeCreateInput,
    ProductUpdateInput,
    VariantCreateInput,
    VariantPriceInput,
    VariantUpdateInput,
)
from sales_api.channels.service import ChannelCreateInput, ChannelUpdateInput
from sales_api.common.errors import ErrorCodes
from sales_api.common.pagination import SortDirection
from sales_api.discounts.model import DiscountValueType, PromotionType
from sales_api.discounts.service import PromotionInput, PromotionRuleInput
from sales_api.inventory.service import WarehouseCreateInput
from sales_api.permissions.model import ANONYMOUS, Permission
from support import BERLIN, D, ShopFixture, expect_error, published


def new_product(f: ShopFixture, name: str, category_id=None):
    return f.shop.catalog.create_product(
        f.owner, ProductCreateInput(product_type_id=f.simple_type.id, name=name, category_id=category_id)
    )


def products(f: ShopFixture, query: ProductQuery | None = None, principal=ANONYMOUS, channel_id=None):
    return f.shop.catalog_queries.products(principal, channel_id or f.channel.id, query or ProductQuery()).data


def test_scheduled_publication(f):
    product = new_product(f, "Future", f.category.id)
    at = f.clock.utc_now + timedelta(days=1)
    f.shop.catalog.update_product_channel_listings(
        f.owner,
        product.id,
        [
            ProductChannelListingInput(
                channel_id=f.channel.id,
                is_published=True,
                published_at=at,
                is_available_for_purchase=True,
                available_for_purchase_at=at,
            )
        ],
    )
    variant = f.shop.catalog.create_variant(
        f.owner,
        VariantCreateInput(
            product_id=product.id,
            channel_listings=(VariantPriceInput(channel_id=f.channel.id, price=D(5)),),
            stocks=((f.warehouse.id, 5),),
        ),
    )

    assert f.shop.catalog_queries.product(ANONYMOUS, f.channel.id, product.slug).data is None
    expect_error(lambda: f.new_checkout((variant, 1)), ErrorCodes.VARIANT_UNAVAILABLE)

    f.clock.advance(timedelta(days=1, seconds=1))
    assert f.shop.catalog_queries.product(ANONYMOUS, f.channel.id, product.slug).data is not None
    assert len(f.new_checkout((variant, 1)).lines) == 1


def test_publishing_without_category_fails(f):
    product = new_product(f, "Orphan")

    expect_error(
        lambda: f.shop.catalog.update_product_channel_listings(f.owner, product.id, [published(f.channel.id)]),
        ErrorCodes.CATEGORY_MISSING,
    )


def test_deleting_category_unpublishes_products_everywhere(f):
    child = f.shop.catalog.create_category(f.owner, CategoryCreateInput(name="Shirts", parent_id=f.category.id))
    second = f.shop.channels.create(f.owner, ChannelCreateInput(name="Second", currency="EUR", default_country="DE"))
    product = new_product(f, "Polo", child.id)
    f.shop.catalog.update_product_channel_listings(f.owner, product.id, [published(f.channel.id), published(second.id)])

    f.shop.catalog.delete_category(f.owner, f.category.id)

    def check(s):
        assert not s.categories
        p = s.products[product.id]
        assert p.category_id is None
        assert all(not listing.is_published for listing in p.channel_listings.values())
        assert len(p.channel_listings) == 2

    f.shop.context.db.read(check)


def test_search_by_prefix_after_rename(f):
    product = new_product(f, "Classic Sneaker", f.category.id)
    f.shop.catalog.update_product_channel_listings(f.owner, product.id, [published(f.channel.id)])
    f.shop.catalog.create_variant(
        f.owner,
        VariantCreateInput(
            product_id=product.id,
            sku="SNK-001",
            channel_listings=(VariantPriceInput(channel_id=f.channel.id, price=D(50)),),
        ),
    )
    f.run_jobs()

    def search(q):
        return products(f, ProductQuery(search=q, sort=ProductSortField.RANK)).items

    f.shop.catalog.update_product(f.owner, product.id, ProductUpdateInput(name="Runner Ultraboost"))
    assert search("ultra") == []  # indexing is asynchronous
    f.run_jobs()

    assert len(search("ultra")) == 1
    assert len(search("RUN ultrab")) == 1
    assert len(search("snk")) == 1
    assert len(search("clothing")) == 1
    assert search("classic") == []


def test_promotion_in_listing_and_sort(f):
    shoes = f.shop.catalog.create_category(f.owner, CategoryCreateInput(name="Shoes"))
    a = f.product("Alpha", 100, category_id=shoes.id)
    f.product("Beta", 90)
    f.run_jobs()
    f.shop.discounts.create_promotion(
        f.owner,
        PromotionInput(
            name="-20% shoes",
            type=PromotionType.CATALOGUE,
            rules=(
                PromotionRuleInput(
                    channel_ids=(f.channel.id,),
                    category_ids=(shoes.id,),
                    value_type=DiscountValueType.PERCENTAGE,
                    value=D(20),
                ),
            ),
        ),
    )
    f.run_jobs()

    page = products(f, ProductQuery(sort=ProductSortField.PRICE))

    assert [p.name for p in page.items] == ["Alpha", "Beta"]
    alpha = page.items[0]
    assert alpha.price_range.start.gross.amount == 80
    assert alpha.price_range_undiscounted.start.gross.amount == 100
    matching = [p for p in products(f, ProductQuery(price_lte=D(85))).items if p.id == a.product_id]
    assert len(matching) == 1


def test_cost_price_without_permission_is_null_with_field_error(f):
    item = f.product("Secret", 10)

    def set_private(s):
        s.products[item.product_id].private_metadata["supplier"] = "ACME"

    f.shop.context.db.write(set_private)
    slug = f.shop.context.db.read(lambda s: s.products[item.product_id].slug)

    anonymous = f.shop.catalog_queries.product(ANONYMOUS, f.channel.id, slug)
    staff = f.shop.catalog_queries.product(
        f.staff_with("cat@example.org", Permission.MANAGE_PRODUCTS), f.channel.id, slug
    )

    assert anonymous.data.variants[0].cost_price is None
    assert anonymous.data.private_metadata is None
    assert anonymous.data.variants[0].price.gross.amount == 10
    assert any(e.code == ErrorCodes.FORBIDDEN and e.field.endswith("costPrice") for e in anonymous.errors)
    assert staff.data.variants[0].cost_price == 5
    assert staff.errors == []


def test_hidden_product_reachable_by_link(f):
    product = new_product(f, "Hidden gem", f.category.id)
    f.shop.catalog.update_product_channel_listings(
        f.owner,
        product.id,
        [
            ProductChannelListingInput(
                channel_id=f.channel.id, is_published=True, is_available_for_purchase=True, visible_in_listings=False
            )
        ],
    )
    variant = f.shop.catalog.create_variant(
        f.owner,
        VariantCreateInput(
            product_id=product.id,
            channel_listings=(VariantPriceInput(channel_id=f.channel.id, price=D(5)),),
            stocks=((f.warehouse.id, 1),),
        ),
    )

    assert products(f).items == []
    assert f.shop.catalog_queries.product(ANONYMOUS, f.channel.id, product.slug).data is not None
    assert len(f.new_checkout((variant, 1)).lines) == 1


def test_variant_without_channel_price_is_not_purchasable(f):
    product = new_product(f, "No price", f.category.id)
    f.shop.catalog.update_product_channel_listings(f.owner, product.id, [published(f.channel.id)])
    variant = f.shop.catalog.create_variant(
        f.owner, VariantCreateInput(product_id=product.id, stocks=((f.warehouse.id, 1),))
    )

    expect_error(lambda: f.new_checkout((variant, 1)))


def test_sku_unique_and_variant_combinations_unique(f):
    shirt = f.shop.catalog.create_product(
        f.owner, ProductCreateInput(product_type_id=f.shirt_type.id, name="Tee", category_id=f.category.id)
    )

    def size(value):
        return (AttributeValueInput(attribute_id=f.size_attribute.id, values=(value,)),)

    f.shop.catalog.create_variant(f.owner, VariantCreateInput(product_id=shirt.id, sku="TEE-M", attributes=size("M")))

    expect_error(
        lambda: f.shop.catalog.create_variant(
            f.owner, VariantCreateInput(product_id=shirt.id, sku="tee-m", attributes=size("L"))
        ),
        ErrorCodes.DUPLICATE,
    )
    expect_error(
        lambda: f.shop.catalog.create_variant(
            f.owner, VariantCreateInput(product_id=shirt.id, sku="TEE-M2", attributes=size("M"))
        ),
        ErrorCodes.VARIANT_DUPLICATE,
    )
    f.shop.catalog.create_variant(f.owner, VariantCreateInput(product_id=shirt.id, attributes=size("L")))


def test_type_without_variant_attributes_has_single_variant(f):
    item = f.product("Single", 1)
    expect_error(lambda: f.shop.catalog.create_variant(f.owner, VariantCreateInput(product_id=item.product_id)))


def test_required_attributes_and_input_types_are_validated(f):
    material = f.shop.catalog.create_attribute(
        f.owner,
        AttributeCreateInput(
            name="Material", scope=AttributeScope.PRODUCT, input_type=AttributeInputType.PLAIN_TEXT, value_required=True
        ),
    )
    length = f.shop.catalog.create_attribute(
        f.owner,
        AttributeCreateInput(
            name="Length", scope=AttributeScope.PRODUCT, input_type=AttributeInputType.NUMERIC, unit=MeasurementUnit.CM
        ),
    )
    related = f.shop.catalog.create_attribute(
        f.owner,
        AttributeCreateInput(
            name="Related",
            scope=AttributeScope.PRODUCT,
            input_type=AttributeInputType.REFERENCE,
            allowed_reference_targets=(ReferenceTarget.PRODUCT,),
        ),
    )
    product_type = f.shop.catalog.create_product_type(
        f.owner, ProductTypeCreateInput(name="Scarf", product_attribute_ids=(material.id, length.id, related.id))
    )

    expect_error(
        lambda: f.shop.catalog.create_product(
            f.owner, ProductCreateInput(product_type_id=product_type.id, name="Scarf")
        ),
        ErrorCodes.REQUIRED,
    )
    expect_error(
        lambda: f.shop.catalog.create_attribute(
            f.owner,
            AttributeCreateInput(
                name="Bad",
                scope=AttributeScope.PRODUCT,
                input_type=AttributeInputType.PLAIN_TEXT,
                unit=MeasurementUnit.KG,
            ),
        )
    )
    expect_error(
        lambda: f.shop.catalog.create_product(
            f.owner,
            ProductCreateInput(
                product_type_id=product_type.id,
                name="Scarf",
                attributes=(
                    AttributeValueInput(attribute_id=material.id, text="Wool"),
                    AttributeValueInput(attribute_id=related.id, references=(f.category.id,)),
                ),
            ),
        )
    )

    other = new_product(f, "Hat")
    scarf = f.shop.catalog.create_product(
        f.owner,
        ProductCreateInput(
            product_type_id=product_type.id,
            name="Scarf",
            attributes=(
                AttributeValueInput(attribute_id=material.id, text='<b onclick="x()">Wool</b>'),
                AttributeValueInput(attribute_id=length.id, number=D(180)),
                AttributeValueInput(attribute_id=related.id, references=(other.id,)),
            ),
        ),
    )
    assert next(a for a in scarf.attributes if a.attribute_id == material.id).text == "<b>Wool</b>"


def test_unassigned_attribute_values_are_hidden(f):
    color = f.shop.catalog.create_attribute(
        f.owner,
        AttributeCreateInput(
            name="Color", scope=AttributeScope.PRODUCT, input_type=AttributeInputType.DROPDOWN, values=("Red",)
        ),
    )
    product_type = f.shop.catalog.create_product_type(
        f.owner, ProductTypeCreateInput(name="Colored", product_attribute_ids=(color.id,))
    )
    item = f.product("Red thing", 3, product_type=product_type)
    f.shop.catalog.update_product(
        f.owner,
        item.product_id,
        ProductUpdateInput(attributes=(AttributeValueInput(attribute_id=color.id, values=("Red",)),)),
    )
    slug = f.shop.context.db.read(lambda s: s.products[item.product_id].slug)
    assert len(f.shop.catalog_queries.product(ANONYMOUS, f.channel.id, slug).data.attributes) == 1
    query = ProductQuery(attribute_values={color.id: ("red",)})
    assert len(products(f, query).items) == 1

    f.shop.catalog.unassign_attributes(f.owner, product_type.id, [color.id])

    assert f.shop.catalog_queries.product(ANONYMOUS, f.channel.id, slug).data.attributes == ()
    assert products(f, query).items == []


def test_filters_sorting_and_stable_pagination(f):
    for i, name in enumerate(["Delta", "Alpha", "Charlie", "Bravo", "Echo"]):
        f.product(name, 10 + i)
    f.run_jobs()

    first = products(f, ProductQuery(first=2))
    assert [p.name for p in first.items] == ["Alpha", "Bravo"]

    f.product("Aardvark", 1)  # inserted before the cursor
    second = products(f, ProductQuery(first=2, after=first.end_cursor))
    assert [p.name for p in second.items] == ["Charlie", "Delta"]

    by_price_desc = products(f, ProductQuery(sort=ProductSortField.PRICE, direction=SortDirection.DESC, first=1))
    assert [p.name for p in by_price_desc.items] == ["Echo"]

    expect_error(lambda: products(f, ProductQuery(first=101)))
    assert products(f, ProductQuery(price_gte=D(11), price_lte=D(13))).total_count == 3


def test_stock_availability_filter_uses_shopper_country(f):
    f.product("In Berlin", 5, stock=3)
    empty = f.product("Sold out", 5, stock=0)

    in_stock_de = products(f, ProductQuery(in_stock=True, country="DE")).items
    in_stock_us = products(f, ProductQuery(in_stock=True, country="US")).items

    assert [p.name for p in in_stock_de] == ["In Berlin"]
    assert in_stock_us == []
    assert all(p.id != empty.product_id for p in in_stock_de)


def test_unpublished_visibility(f):
    new_product(f, "Draft", f.category.id)

    assert products(f).items == []
    assert len(products(f, principal=f.owner).items) == 1

    f.shop.channels.update(f.owner, f.channel.id, ChannelUpdateInput(is_active=False))
    expect_error(lambda: products(f))


def test_products_of_category_include_subcategories(f):
    child = f.shop.catalog.create_category(f.owner, CategoryCreateInput(name="Kids", parent_id=f.category.id))
    grand_child = f.shop.catalog.create_category(f.owner, CategoryCreateInput(name="Babies", parent_id=child.id))
    f.product("Onesie", 5, category_id=grand_child.id)
    f.product("Jacket", 5)

    assert products(f, ProductQuery(category_ids=(f.category.id,))).total_count == 2
    assert products(f, ProductQuery(category_ids=(f.category.id,), include_subcategories=False)).total_count == 1


def test_slugs_are_generated_transliterated_and_unique(f):
    a = new_product(f, "Футболка базовая")
    b = new_product(f, "Футболка базовая")

    assert a.slug == "futbolka-bazovaya"
    assert b.slug == "futbolka-bazovaya-2"
    expect_error(
        lambda: f.shop.catalog.create_product(
            f.owner, ProductCreateInput(product_type_id=f.simple_type.id, name="X", slug="futbolka-bazovaya")
        )
    )


def test_weight_falls_back_variant_product_type_zero(f):
    shirt = f.product("Shirt", 10, product_type=f.shirt_type)
    checkout = f.new_checkout((shirt, 2))
    assert f.shop.checkouts.get(checkout.token).pricing.weight_kg == D("0.6")

    f.shop.catalog.update_variant(f.owner, shirt.id, VariantUpdateInput(weight_kg=D(1)))
    assert f.shop.checkouts.get_pricing(checkout.token, force_recalculate=True).weight_kg == 2


def test_rich_text_is_sanitized_on_save(f):
    description = json.loads(
        """
        {"blocks":[{"type":"paragraph","data":{"text":"Hi <script>alert(1)</script><a href=\\"javascript:evil()\\" onmouseover=\\"x()\\">link</a>"}},
                   {"type":"embed","data":{"url":"javascript:alert(1)","onload":"bad"}}]}
        """
    )

    product = f.shop.catalog.create_product(
        f.owner, ProductCreateInput(product_type_id=f.simple_type.id, name="Safe", description=description)
    )

    text = json.dumps(product.description).lower()
    assert "script" not in text
    assert "javascript" not in text
    assert "onmouseover" not in text
    assert "onload" not in text
    assert "link" in text
    expect_error(
        lambda: f.shop.catalog.create_product(
            f.owner,
            ProductCreateInput(
                product_type_id=f.simple_type.id, name="Bad", description={"blocks": [{"type": "iframe"}]}
            ),
        )
    )


@pytest.mark.parametrize("requested,expected", [(1, 32), (32, 32), (33, 64), (300, 512), (9000, 4096)])
def test_thumbnail_sizes_round_up(requested, expected):
    assert thumbnail_size(requested) == expected


def test_catalog_mutations_require_permission(f):
    no_catalog = f.staff_with("nocat@example.org", Permission.MANAGE_ORDERS)
    expect_error(
        lambda: f.shop.catalog.create_product(
            no_catalog, ProductCreateInput(product_type_id=f.simple_type.id, name="X")
        ),
        ErrorCodes.FORBIDDEN,
    )
    expect_error(lambda: f.shop.catalog.create_product_type(no_catalog, ProductTypeCreateInput(name="T")))
    expect_error(lambda: f.shop.inventory.create_warehouse(ANONYMOUS, WarehouseCreateInput(name="W", address=BERLIN)))
