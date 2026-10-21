from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum, auto
from typing import Any

from ..channels.model import Channel
from ..checkout import rules as checkout_rules
from ..common.errors import DomainError, DomainException, ErrorCodes, must_exist
from ..common.metadata import matches_filter
from ..common.money import Money, TaxedMoney
from ..common.pagination import Page, SortDirection, paginate
from ..inventory import stock_logic
from ..inventory.stock_logic import StockScope
from ..permissions.model import Permission, Principal
from ..persistence.store import Store
from ..shop_context import ShopContext
from ..taxes.calculator import TaxCalculator
from .indexer import CatalogIndexer, thumbnail_url
from .model import AssignedAttribute, MediaType, Product, ProductType, ProductVariant
from .service import category_with_descendants


@dataclass(frozen=True)
class QueryResult[T]:
    data: T
    errors: list[DomainError]


@dataclass(frozen=True)
class PriceRange:
    start: TaxedMoney
    stop: TaxedMoney


@dataclass(frozen=True)
class VariantView:
    id: uuid.UUID
    name: str
    sku: str | None
    price: TaxedMoney | None
    undiscounted_price: TaxedMoney | None
    cost_price: Decimal | None
    is_purchasable: bool


@dataclass(frozen=True, kw_only=True)
class ProductView:
    id: uuid.UUID
    name: str
    slug: str
    category_id: uuid.UUID | None = None
    price_range: PriceRange | None = None
    price_range_undiscounted: PriceRange | None = None
    displayed_price_from: Money | None = None
    is_available_for_purchase: bool = False
    is_in_stock: bool = False
    thumbnail_url: str | None = None
    attributes: tuple[AssignedAttribute, ...] = ()
    variants: tuple[VariantView, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)
    private_metadata: Mapping[str, str] | None = None
    rating: Decimal | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ProductSortField(StrEnum):
    NAME = auto()
    PRICE = auto()
    MINIMAL_PRICE = auto()
    PUBLISHED_AT = auto()
    CREATED_AT = auto()
    UPDATED_AT = auto()
    RATING = auto()
    RANK = auto()
    ATTRIBUTE = auto()


@dataclass(frozen=True, kw_only=True)
class ProductQuery:
    search: str | None = None
    category_ids: tuple[uuid.UUID, ...] = ()
    include_subcategories: bool = True
    collection_ids: tuple[uuid.UUID, ...] = ()
    product_type_ids: tuple[uuid.UUID, ...] = ()
    price_gte: Decimal | None = None
    price_lte: Decimal | None = None
    attribute_values: Mapping[uuid.UUID, tuple[str, ...]] = field(default_factory=dict)
    attribute_ranges: Mapping[uuid.UUID, tuple[Decimal | None, Decimal | None]] = field(default_factory=dict)
    in_stock: bool | None = None
    is_available: bool | None = None
    updated_from: datetime | None = None
    created_from: datetime | None = None
    metadata: tuple[str, str | None] | None = None
    ids: tuple[uuid.UUID, ...] = ()
    skus: tuple[str, ...] = ()
    sort: ProductSortField = ProductSortField.NAME
    sort_attribute_id: uuid.UUID | None = None
    direction: SortDirection = SortDirection.ASC
    first: int = 20
    after: str | None = None
    country: str | None = None
    thumbnail_size: int = 256


class CatalogQueryService:
    def __init__(self, ctx: ShopContext, indexer: CatalogIndexer):
        self._ctx = ctx
        self._indexer = indexer

    def products(
        self, principal: Principal, channel_id: uuid.UUID, query: ProductQuery
    ) -> QueryResult[Page[ProductView]]:
        def run(store: Store) -> QueryResult[Page[ProductView]]:
            channel = _resolve_channel(store, principal, channel_id)
            now = self._ctx.now
            staff = principal.has(Permission.MANAGE_PRODUCTS)
            ranks = dict(self._indexer.search(query.search)) if query.search else None

            if not query.category_ids:
                category_filter = None
            elif query.include_subcategories:
                category_filter = {c for root in query.category_ids for c in category_with_descendants(store, root)}
            else:
                category_filter = set(query.category_ids)
            country = query.country or channel.default_country
            stock_scope = _stock_scope_for(store, channel, country)
            collection_filter = None
            if query.collection_ids:
                collection_filter = {
                    pid
                    for id_ in dict.fromkeys(query.collection_ids)
                    if (col := store.collections.get(id_)) is not None
                    for pid in col.product_ids
                }
            type_filter = set(query.product_type_ids) or None
            id_filter = set(query.ids) or None
            sku_filter = {s.upper() for s in query.skus} or None

            def matches(p: Product) -> bool:
                if not (staff or _is_listed(p, channel, now)):
                    return False
                if ranks is not None and p.id not in ranks:
                    return False
                if category_filter is not None and (p.category_id is None or p.category_id not in category_filter):
                    return False
                if collection_filter is not None and p.id not in collection_filter:
                    return False
                if type_filter is not None and p.product_type_id not in type_filter:
                    return False
                if id_filter is not None and p.id not in id_filter:
                    return False
                if sku_filter is not None and not any(
                    v.sku is not None and v.sku.upper() in sku_filter for v in store.variants.of_product(p.id)
                ):
                    return False
                if query.updated_from is not None and not p.updated_at >= query.updated_from:
                    return False
                if query.created_from is not None and not p.created_at >= query.created_from:
                    return False
                if query.metadata is not None and not matches_filter(p.metadata, query.metadata[0], query.metadata[1]):
                    return False
                if not _matches_attributes(store, p, query):
                    return False
                if not _matches_price(store, p, channel, query):
                    return False
                if query.is_available is not None and _is_purchasable(store, p, channel, now) != query.is_available:
                    return False
                return not (
                    query.in_stock is not None and _is_in_stock(store, p, channel, stock_scope, now) != query.in_stock
                )

            candidates = [p for p in store.products.values() if matches(p)]

            page = paginate(
                candidates,
                lambda p: _sort_key(store, p, channel, query, ranks),
                query.first,
                query.after,
                query.direction,
            )
            errors: list[DomainError] = []
            items = [
                _build_view(store, principal, p, channel, country, stock_scope, query.thumbnail_size, now, errors)
                for p in page.items
            ]
            return QueryResult(Page(items, page.end_cursor, page.has_next_page, page.total_count), errors)

        return self._ctx.db.read(run)

    def product(
        self, principal: Principal, channel_id: uuid.UUID, slug: str, country: str | None = None
    ) -> QueryResult[ProductView | None]:
        def run(store: Store) -> QueryResult[ProductView | None]:
            channel = _resolve_channel(store, principal, channel_id)
            product = store.products.find_by_slug(slug)
            if product is None or (
                not principal.has(Permission.MANAGE_PRODUCTS) and not is_visible(product, channel, self._ctx.now)
            ):
                return QueryResult(None, [])
            errors: list[DomainError] = []
            resolved_country = country or channel.default_country
            view = _build_view(
                store,
                principal,
                product,
                channel,
                resolved_country,
                _stock_scope_for(store, channel, resolved_country),
                256,
                self._ctx.now,
                errors,
            )
            return QueryResult(view, errors)

        return self._ctx.db.read(run)


def _resolve_channel(store: Store, principal: Principal, channel_id: uuid.UUID) -> Channel:
    channel = must_exist(store.channels.get(channel_id), "channel", channel_id)
    if not channel.is_active and not principal.is_staff_or_app:
        raise DomainException(ErrorCodes.CHANNEL_INACTIVE, "channel", "Channel is inactive.")
    return channel


def is_visible(p: Product, channel: Channel, now: datetime) -> bool:
    listing = p.channel_listings.get(channel.id)
    return listing is not None and listing.is_visible_at(now)


def _is_listed(p: Product, channel: Channel, now: datetime) -> bool:
    listing = p.channel_listings.get(channel.id)
    return listing is not None and listing.is_visible_at(now) and listing.visible_in_listings


def _is_purchasable(store: Store, p: Product, channel: Channel, now: datetime) -> bool:
    return any(checkout_rules.unavailability_reason(store, channel, v, now) is None for v in _variants_of(store, p))


def _is_in_stock(store: Store, p: Product, channel: Channel, scope: StockScope, now: datetime) -> bool:
    return any(
        not stock_logic.find_shortages(store, channel, scope, [(v.id, 1)], now, None) for v in _variants_of(store, p)
    )


def _stock_scope_for(store: Store, channel: Channel, country: str) -> StockScope:
    return StockScope(tuple(stock_logic.channel_warehouses_serving(store, channel, country)), country)


def _variants_of(store: Store, p: Product) -> list[ProductVariant]:
    return sorted(store.variants.of_product(p.id), key=lambda v: v.sort_order)


def _min_discounted_price(store: Store, p: Product, channel: Channel) -> Decimal | None:
    prices = []
    for v in _variants_of(store, p):
        listing = v.channel_listings.get(channel.id)
        if listing is not None and listing.price is not None:
            prices.append(listing.discounted_price if listing.discounted_price is not None else listing.price)
    return min(prices) if prices else None


def _matches_price(store: Store, p: Product, channel: Channel, q: ProductQuery) -> bool:
    if q.price_gte is None and q.price_lte is None:
        return True
    price = _min_discounted_price(store, p, channel)
    return (
        price is not None
        and (q.price_gte is None or price >= q.price_gte)
        and (q.price_lte is None or price <= q.price_lte)
    )


def _matches_attributes(store: Store, p: Product, q: ProductQuery) -> bool:
    if not q.attribute_values and not q.attribute_ranges:
        return True
    product_type = store.product_types[p.product_type_id]
    assigned = _visible_attributes(
        product_type, [*p.attributes, *(a for v in _variants_of(store, p) for a in v.attributes)]
    )

    for attribute_id, slugs in q.attribute_values.items():
        attribute = store.attributes.get(attribute_id)
        if attribute is None:
            return False
        value_ids = {v.id for v in attribute.values if v.slug in slugs}
        if not any(
            a.attribute_id == attribute_id
            and (
                any(v in value_ids for v in a.value_ids)
                or (a.boolean is not None and ("true" if a.boolean else "false") in slugs)
            )
            for a in assigned
        ):
            return False

    for attribute_id, (gte, lte) in q.attribute_ranges.items():
        if not any(
            a.attribute_id == attribute_id
            and a.number is not None
            and (gte is None or a.number >= gte)
            and (lte is None or a.number <= lte)
            for a in assigned
        ):
            return False

    return True


def _visible_attributes(kind: ProductType, attributes: Iterable[AssignedAttribute]) -> list[AssignedAttribute]:
    allowed = set(kind.product_attribute_ids) | set(kind.variant_attribute_ids)
    return [a for a in attributes if a.attribute_id in allowed]


def _sort_key(
    store: Store, p: Product, channel: Channel, q: ProductQuery, ranks: dict[uuid.UUID, int] | None
) -> list[Any]:
    match q.sort:
        case ProductSortField.PRICE | ProductSortField.MINIMAL_PRICE:
            return [_min_discounted_price(store, p, channel), p.id]
        case ProductSortField.PUBLISHED_AT:
            listing = p.channel_listings.get(channel.id)
            return [listing.published_at if listing is not None else None, p.id]
        case ProductSortField.CREATED_AT:
            return [p.created_at, p.id]
        case ProductSortField.UPDATED_AT:
            return [p.updated_at, p.id]
        case ProductSortField.RATING:
            return [p.rating, p.id]
        case ProductSortField.RANK:
            return [-(ranks.get(p.id, 0) if ranks is not None else 0), p.id]
        case ProductSortField.ATTRIBUTE:
            return [_attribute_sort_value(store, p, q.sort_attribute_id), p.id]
        case _:
            return [p.name.casefold(), p.id]


def _format_number(n: Decimal) -> str:
    # zero-padded so that strings sort like numbers
    text = f"{abs(n):.4f}"
    whole, frac = text.split(".")
    return ("-" if n < 0 else "") + whole.zfill(12) + "." + frac


def _attribute_sort_value(store: Store, p: Product, attribute_id: uuid.UUID | None) -> str | None:
    attribute = store.attributes.get(attribute_id) if attribute_id is not None else None
    if attribute is None:
        return None
    assigned = next((a for a in p.attributes if a.attribute_id == attribute_id), None)
    if assigned is None:
        return None
    values = [
        v
        for value_id in assigned.value_ids
        if (v := next((x for x in attribute.values if x.id == value_id), None)) is not None
    ]
    values.sort(key=lambda v: v.sort_order)
    if values:
        return values[0].name
    if assigned.text is not None:
        return assigned.text
    return _format_number(assigned.number) if assigned.number is not None else None


def _build_view(
    store: Store,
    principal: Principal,
    p: Product,
    channel: Channel,
    country: str,
    stock_scope: StockScope,
    thumbnail_size: int,
    now: datetime,
    errors: list[DomainError],
) -> ProductView:
    product_type = store.product_types[p.product_type_id]
    can_see_internal = principal.has(Permission.MANAGE_PRODUCTS)
    tax_class = p.tax_class_id or product_type.tax_class_id

    variants: list[VariantView] = []
    for v in _variants_of(store, p):
        listing = v.channel_listings.get(channel.id)
        price = undiscounted = None
        if listing is not None and listing.price is not None:
            base = listing.discounted_price if listing.discounted_price is not None else listing.price
            price = TaxCalculator.catalog_price(store, channel, base, tax_class, country)
            undiscounted = TaxCalculator.catalog_price(store, channel, listing.price, tax_class, country)
        if not can_see_internal and listing is not None and listing.cost_price is not None:
            errors.append(
                DomainError(
                    ErrorCodes.FORBIDDEN,
                    f"products.{p.id}.variants.{v.id}.costPrice",
                    "Requires manage_products.",
                )
            )
        variants.append(
            VariantView(
                v.id,
                v.name,
                v.sku,
                price,
                undiscounted,
                listing.cost_price if can_see_internal and listing is not None else None,
                checkout_rules.unavailability_reason(store, channel, v, now) is None,
            )
        )

    def price_range(values: list[TaxedMoney]) -> PriceRange | None:
        if not values:
            return None
        return PriceRange(min(values, key=lambda m: m.gross.amount), max(values, key=lambda m: m.gross.amount))

    range_ = price_range([v.price for v in variants if v.price is not None])
    if not can_see_internal and p.private_metadata:
        errors.append(
            DomainError(ErrorCodes.FORBIDDEN, f"products.{p.id}.privateMetadata", "Requires manage_products.")
        )

    images = sorted((m for m in p.media if m.type == MediaType.IMAGE), key=lambda m: m.sort_order)
    thumbnail = images[0] if images else None
    displayed = None
    if range_ is not None:
        displayed = range_.start.gross if channel.taxes.display_gross_for(country) else range_.start.net
    return ProductView(
        id=p.id,
        name=p.name,
        slug=p.slug,
        category_id=p.category_id,
        price_range=range_,
        price_range_undiscounted=price_range(
            [v.undiscounted_price for v in variants if v.undiscounted_price is not None]
        ),
        displayed_price_from=displayed,
        is_available_for_purchase=any(v.is_purchasable for v in variants),
        is_in_stock=_is_in_stock(store, p, channel, stock_scope, now),
        thumbnail_url=None if thumbnail is None else thumbnail_url(thumbnail.url, thumbnail_size),
        attributes=tuple(_visible_attributes(product_type, p.attributes)),
        variants=tuple(variants),
        metadata=dict(p.metadata),
        private_metadata=dict(p.private_metadata) if can_see_internal else None,
        rating=p.rating,
        created_at=p.created_at,
        updated_at=p.updated_at,
    )
