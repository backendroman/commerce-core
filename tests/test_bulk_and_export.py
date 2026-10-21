import uuid
from datetime import timedelta

from sales_api.bulk.export import ExportStatus, ProductExportInput
from sales_api.bulk.service import BulkErrorPolicy
from sales_api.catalog.service import ProductCreateInput, VariantCreateInput
from sales_api.common.errors import ErrorCodes
from sales_api.notifications import NotificationType
from support import ShopFixture, expect_error


def ten_variants_with_two_duplicate_skus(f: ShopFixture) -> list[VariantCreateInput]:
    rows = []
    for i in range(10):
        product = f.shop.catalog.create_product(
            f.owner, ProductCreateInput(product_type_id=f.simple_type.id, name=f"P{i}")
        )
        rows.append(VariantCreateInput(product_id=product.id, sku="DUP" if i >= 8 else f"SKU-{i}"))
    return rows


def test_partial_batch_creates_valid_rows(f):
    result = f.shop.bulk.create_variants(f.owner, ten_variants_with_two_duplicate_skus(f), BulkErrorPolicy.PARTIAL)

    assert result.count == 8
    assert sum(1 for r in result.rows if any(e.code == ErrorCodes.DUPLICATE for e in r.errors)) == 2


def test_atomic_batch_creates_nothing(f):
    rows = ten_variants_with_two_duplicate_skus(f)

    result = f.shop.bulk.create_variants(f.owner, rows, BulkErrorPolicy.ATOMIC)

    assert result.count == 0
    assert f.shop.context.db.read(lambda s: len(s.variants)) == 0


def test_batch_size_is_limited_to_100(f):
    rows = [VariantCreateInput(product_id=uuid.uuid4()) for _ in range(101)]
    expect_error(lambda: f.shop.bulk.create_variants(f.owner, rows, BulkErrorPolicy.PARTIAL), ErrorCodes.LIMIT_EXCEEDED)


def test_export_with_prices_and_stocks_notifies_staff(f):
    item = f.product("Exported", "12.5", stock=7, sku="EXP-1")
    f.product("Other", 1)

    export = f.shop.exports.export_products(
        f.owner,
        ProductExportInput(
            product_ids=(item.product_id,), channel_ids=(f.channel.id,), warehouse_ids=(f.warehouse.id,)
        ),
    )
    assert export.status == ExportStatus.PENDING
    f.run_jobs()

    assert export.status == ExportStatus.SUCCESS
    csv = f.shop.file_storage.read(export.file_url).decode("utf-8")
    assert "EXP-1" in csv
    assert "12.5" in csv
    assert "Other" not in csv
    assert any(m.type == NotificationType.EXPORT_READY for m in f.shop.context.mail.to("admin@example.com"))

    f.clock.advance(timedelta(days=31))
    assert f.shop.exports.delete_expired_files() == 1
