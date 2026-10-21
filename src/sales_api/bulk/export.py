from __future__ import annotations

import dataclasses
import logging
import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum, auto
from typing import Protocol

from ..catalog.model import ProductVariant
from ..catalog.queries import CatalogQueryService, ProductQuery
from ..common import rich_text
from ..common.errors import DomainException, must_exist
from ..common.money import fmt
from ..common.pagination import MAX_PAGE_SIZE
from ..notifications import NotificationType, Notifier
from ..permissions.model import Permission, Principal
from ..persistence.store import Store
from ..shop_context import ShopContext, require_permission, require_user

_log = logging.getLogger(__name__)


class ExportStatus(StrEnum):
    PENDING = auto()
    IN_PROGRESS = auto()
    SUCCESS = auto()
    FAILED = auto()


@dataclass(frozen=True)
class ExportEvent:
    at: datetime
    type: str
    message: str | None = None


@dataclass(eq=False, kw_only=True)
class ExportFile:
    requested_by_user_id: uuid.UUID
    kind: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    status: ExportStatus = ExportStatus.PENDING
    file_url: str | None = None
    created_at: datetime
    events: list[ExportEvent] = field(default_factory=list)


class FileStorage(Protocol):
    def save(self, name: str, content: bytes, now: datetime) -> str: ...

    def read(self, url: str) -> bytes | None: ...

    def delete(self, url: str) -> None: ...

    def list(self) -> list[tuple[str, datetime]]: ...


class InMemoryFileStorage:
    def __init__(self) -> None:
        self._files: dict[str, tuple[bytes, datetime]] = {}
        self._lock = threading.Lock()

    def save(self, name: str, content: bytes, now: datetime) -> str:
        url = f"/media/exports/{uuid.uuid4().hex}/{name}"
        with self._lock:
            self._files[url] = (content, now)
        return url

    def read(self, url: str) -> bytes | None:
        with self._lock:
            f = self._files.get(url)
        return f[0] if f is not None else None

    def delete(self, url: str) -> None:
        with self._lock:
            self._files.pop(url, None)

    def list(self) -> list[tuple[str, datetime]]:
        with self._lock:
            return [(url, saved_at) for url, (_, saved_at) in self._files.items()]


@dataclass(frozen=True, kw_only=True)
class ProductExportInput:
    product_ids: tuple[uuid.UUID, ...] = ()
    filter: ProductQuery | None = None
    filter_channel_id: uuid.UUID | None = None
    fields: tuple[str, ...] = ("name", "sku", "variantName")
    attribute_ids: tuple[uuid.UUID, ...] = ()
    channel_ids: tuple[uuid.UUID, ...] = ()
    warehouse_ids: tuple[uuid.UUID, ...] = ()


def _bool(value: bool) -> str:
    return "true" if value else "false"


def _escape(value: str) -> str:
    if any(c in value for c in ',"\n\r'):
        return '"' + value.replace('"', '""') + '"'
    return value


def _csv(header: Iterable[str], rows: Iterable[Sequence[str]]) -> str:
    out = [",".join(_escape(h) for h in header)]
    out.extend(",".join(_escape(c) for c in row) for row in rows)
    return "\n".join(out) + "\n"


class ExportService:
    def __init__(
        self, ctx: ShopContext, catalog_queries: CatalogQueryService, notifier: Notifier, storage: FileStorage
    ):
        self._ctx = ctx
        self._catalog_queries = catalog_queries
        self._notifier = notifier
        self._storage = storage
        self._exports: dict[uuid.UUID, ExportFile] = {}

    def get(self, id_: uuid.UUID) -> ExportFile:
        return self._exports[id_]

    def export_products(self, principal: Principal, data: ProductExportInput) -> ExportFile:
        require_permission(principal, Permission.MANAGE_PRODUCTS)
        return self._start(principal, "products", lambda: self._build_products_csv(principal, data))

    def export_gift_cards(self, principal: Principal) -> ExportFile:
        require_permission(principal, Permission.MANAGE_GIFT_CARDS)

        def build() -> str:
            return self._ctx.db.read(
                lambda store: _csv(
                    ["code", "currency", "initial_balance", "current_balance", "expiry_date", "is_active", "tags"],
                    (
                        [
                            g.code,
                            g.currency,
                            fmt(g.initial_balance),
                            fmt(g.current_balance),
                            g.expiry_date.isoformat() if g.expiry_date is not None else "",
                            _bool(g.is_active),
                            ";".join(g.tags),
                        ]
                        for g in store.gift_cards.values()
                    ),
                )
            )

        return self._start(principal, "gift_cards", build)

    def export_voucher_codes(self, principal: Principal, voucher_id: uuid.UUID) -> ExportFile:
        require_permission(principal, Permission.MANAGE_DISCOUNTS)

        def build() -> str:
            def query(store: Store) -> str:
                voucher = must_exist(store.vouchers.get(voucher_id), "voucher", voucher_id)
                return _csv(
                    ["code", "used", "is_active"], ([c.code, str(c.used), _bool(c.is_active)] for c in voucher.codes)
                )

            return self._ctx.db.read(query)

        return self._start(principal, "voucher_codes", build)

    def delete_expired_files(self) -> int:
        expired = [
            url
            for url, saved_at in self._storage.list()
            if self._ctx.now - saved_at > self._ctx.options.export_file_ttl
        ]
        for url in expired:
            self._storage.delete(url)
        return len(expired)

    def _start(self, principal: Principal, kind: str, build: Callable[[], str]) -> ExportFile:
        export = ExportFile(requested_by_user_id=require_user(principal), kind=kind, created_at=self._ctx.now)
        export.events.append(ExportEvent(self._ctx.now, "QUEUED"))
        self._exports[export.id] = export
        self._ctx.jobs.enqueue(f"export:{export.id}", lambda: self._run(export, build))
        return export

    def _run(self, export: ExportFile, build: Callable[[], str]) -> None:
        ctx = self._ctx
        export.status = ExportStatus.IN_PROGRESS
        user = ctx.db.read(lambda store: store.users.get(export.requested_by_user_id))
        try:
            content = build()
            export.file_url = self._storage.save(
                f"{export.kind}_{ctx.now:%Y%m%d%H%M%S}.csv", content.encode("utf-8"), ctx.now
            )
            export.status = ExportStatus.SUCCESS
            export.events.append(ExportEvent(ctx.now, "DONE"))
            if user is not None:
                self._notifier.send(
                    NotificationType.EXPORT_READY, user.email, user.language_code, {"link": export.file_url}
                )
        except (DomainException, OSError, RuntimeError) as ex:
            _log.warning("Export %s failed", export.id, exc_info=True)
            export.status = ExportStatus.FAILED
            export.events.append(ExportEvent(ctx.now, "FAILED", str(ex)))
            if user is not None:
                self._notifier.send(NotificationType.EXPORT_FAILED, user.email, user.language_code, {"error": str(ex)})

    def _build_products_csv(self, principal: Principal, data: ProductExportInput) -> str:
        if not data.product_ids and data.filter is not None and data.filter_channel_id is not None:
            # not inside db.read, the query service locks by itself
            filtered = self._all_filtered(principal, data.filter_channel_id, data.filter)
        else:
            filtered = None

        def query(store: Store) -> str:
            if data.product_ids:
                product_ids = set(data.product_ids)
            elif filtered is not None:
                product_ids = filtered
            else:
                product_ids = set(store.products.keys())

            header = ["product_id", "variant_id", *data.fields]
            header.extend(f"attribute:{store.attributes[a].slug}" for a in data.attribute_ids)
            for c in data.channel_ids:
                slug = store.channels[c].slug
                header.extend([f"{slug}:price", f"{slug}:currency", f"{slug}:published", f"{slug}:available"])
            header.extend(f"stock:{store.warehouses[w].slug}" for w in data.warehouse_ids)

            variants = sorted(
                (v for v in store.variants.values() if v.product_id in product_ids),
                key=lambda v: (store.products[v.product_id].name, v.sort_order),
            )
            return _csv(header, (_product_row(store, v, data) for v in variants))

        return self._ctx.db.read(query)

    def _all_filtered(self, principal: Principal, channel_id: uuid.UUID, criteria: ProductQuery) -> set[uuid.UUID]:
        ids: set[uuid.UUID] = set()
        after: str | None = None
        while True:
            page = self._catalog_queries.products(
                principal, channel_id, dataclasses.replace(criteria, first=MAX_PAGE_SIZE, after=after)
            ).data
            ids.update(p.id for p in page.items)
            after = page.end_cursor if page.has_next_page else None
            if after is None:
                return ids


def _product_row(store: Store, v: ProductVariant, data: ProductExportInput) -> list[str]:
    p = store.products[v.product_id]

    def field_value(f: str) -> str:
        match f:
            case "name":
                return p.name
            case "slug":
                return p.slug
            case "sku":
                return v.sku or ""
            case "variantName":
                return v.name
            case "category":
                category = store.categories.get(p.category_id) if p.category_id is not None else None
                return category.name if category is not None else ""
            case "productType":
                return store.product_types[p.product_type_id].name
            case "weight":
                weight = v.weight_kg if v.weight_kg is not None else p.weight_kg
                return fmt(weight if weight is not None else Decimal(0))
            case "description":
                return rich_text.to_plain_text(p.description)
        return ""

    cells = [str(p.id), str(v.id), *(field_value(f) for f in data.fields)]
    for attribute_id in data.attribute_ids:
        attribute = store.attributes[attribute_id]
        assigned = next((a for a in [*p.attributes, *v.attributes] if a.attribute_id == attribute_id), None)
        if assigned is None:
            cells.append("")
            continue
        names = ";".join(next(x for x in attribute.values if x.id == id_).name for id_ in assigned.value_ids)
        cells.append(names + (assigned.text or "") + (fmt(assigned.number) if assigned.number is not None else ""))

    for c in data.channel_ids:
        listing = v.channel_listings.get(c)
        product_listing = p.channel_listings.get(c)
        cells.extend(
            [
                fmt(listing.price) if listing is not None and listing.price is not None else "",
                store.channels[c].currency,
                _bool(product_listing is not None and product_listing.is_published),
                _bool(product_listing is not None and product_listing.is_available_for_purchase),
            ]
        )

    for w in data.warehouse_ids:
        stock = store.find_stock(w, v.id)
        cells.append(str(stock.quantity_in_stock) if stock is not None else "")
    return cells
