from __future__ import annotations

import bisect
import dataclasses
import re
import threading
import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING

from ..common import rich_text
from ..discounts import catalogue as catalogue_discounts
from ..persistence.store import Store
from .model import Product

if TYPE_CHECKING:
    from ..discounts.model import Promotion
    from ..shop_context import ShopContext

_WORD = re.compile(r"[^\W_]+")


def tokenize(text: str) -> Iterator[str]:
    return (m.group(0) for m in _WORD.finditer(text.lower()))


class CatalogIndexer:
    def __init__(self, ctx: ShopContext):
        self._ctx = ctx
        self._gate = threading.RLock()
        self._dirty: set[uuid.UUID] = set()
        self._documents: dict[uuid.UUID, dict[str, int]] = {}
        # guarded by _gate
        self._postings: dict[str, dict[uuid.UUID, int]] = {}
        self._tokens: list[str] = []  # sorted

    def schedule_product(self, product_id: uuid.UUID) -> None:
        with self._gate:
            self._dirty.add(product_id)
        self._ctx.jobs.enqueue("catalog-index", self.process_dirty)

    def schedule_all(self) -> None:
        ids = self._ctx.db.read(lambda store: list(store.products.keys()))
        with self._gate:
            self._dirty.update(ids)
        self._ctx.jobs.enqueue("catalog-index", self.process_dirty)

    def remove(self, product_id: uuid.UUID) -> None:
        with self._gate:
            self._dirty.discard(product_id)
            self._remove_document(product_id)

    def process_dirty(self) -> None:
        with self._gate:
            batch = list(self._dirty)
            self._dirty.clear()

        def index(store: Store) -> None:
            active_promotions = catalogue_discounts.active_promotions(store, self._ctx.now)
            for product_id in batch:
                product = store.products.get(product_id)
                if product is None:
                    continue
                document = _build_document(store, product)
                with self._gate:
                    self._set_document(product_id, document)
                _recompute_discounted_prices(store, product, active_promotions)

        self._ctx.db.write(index)

    def search(self, query: str) -> list[tuple[uuid.UUID, int]]:
        terms = list(dict.fromkeys(tokenize(query)))
        if not terms:
            return []
        with self._gate:
            # All terms must match. Score is the sum of the best prefix weights.
            totals: dict[uuid.UUID, int] | None = None
            for term in terms:
                best: dict[uuid.UUID, int] = {}
                i = bisect.bisect_left(self._tokens, term)
                while i < len(self._tokens) and self._tokens[i].startswith(term):
                    for product_id, weight in self._postings[self._tokens[i]].items():
                        if totals is not None and product_id not in totals:
                            continue
                        if weight > best.get(product_id, 0):
                            best[product_id] = weight
                    i += 1

                if totals is not None:
                    for product_id in best:
                        best[product_id] += totals[product_id]
                totals = best
                if not totals:
                    return []

            return sorted((totals or {}).items(), key=lambda kv: -kv[1])

    def _set_document(self, product_id: uuid.UUID, document: dict[str, int]) -> None:
        self._remove_document(product_id)
        self._documents[product_id] = document
        for token, weight in document.items():
            products = self._postings.get(token)
            if products is None:
                products = self._postings[token] = {}
                bisect.insort(self._tokens, token)
            products[product_id] = weight

    def _remove_document(self, product_id: uuid.UUID) -> None:
        document = self._documents.pop(product_id, None)
        if document is None:
            return
        for token in document:
            products = self._postings[token]
            products.pop(product_id, None)
            if products:
                continue
            del self._postings[token]
            i = bisect.bisect_left(self._tokens, token)
            if i < len(self._tokens) and self._tokens[i] == token:
                del self._tokens[i]


def _build_document(store: Store, product: Product) -> dict[str, int]:
    weights: dict[str, int] = {}

    def add(text: str | None, weight: int) -> None:
        for token in tokenize(text or ""):
            weights[token] = max(weights.get(token, 0), weight)

    add(product.name, 10)
    add(rich_text.to_plain_text(product.description), 3)
    product_type = store.product_types.get(product.product_type_id)
    add(product_type.name if product_type else None, 2)
    if product.category_id is not None:
        category = store.categories.get(product.category_id)
        add(category.name if category else None, 2)

    visible_attribute_ids = (
        set()
        if product_type is None
        else set(product_type.product_attribute_ids) | set(product_type.variant_attribute_ids)
    )
    variants = store.variants.of_product(product.id)
    for variant in variants:
        add(variant.sku, 8)
        add(variant.name, 4)

    for assigned in [*product.attributes, *(a for v in variants for a in v.attributes)]:
        attribute = store.attributes.get(assigned.attribute_id)
        if assigned.attribute_id not in visible_attribute_ids or attribute is None:
            continue
        weight = max(1, attribute.search_weight)
        for value_id in assigned.value_ids:
            value = next((v for v in attribute.values if v.id == value_id), None)
            add(value.name if value else None, weight)
        add(assigned.text, weight)
        add(None if assigned.number is None else format(assigned.number, "f"), weight)

    return weights


def _recompute_discounted_prices(store: Store, product: Product, active_promotions: list[Promotion]) -> None:
    for variant in store.variants.of_product(product.id):
        for channel_id, listing in list(variant.channel_listings.items()):
            channel = store.channels.get(channel_id)
            if listing.price is None or channel is None:
                continue
            discounted = catalogue_discounts.discounted_unit_price(
                store, variant, channel, listing.price, active_promotions
            ).price
            variant.channel_listings[channel_id] = dataclasses.replace(listing, discounted_price=discounted)


THUMBNAIL_SIZES = (32, 64, 128, 256, 512, 1024, 2048, 4096)


def thumbnail_size(requested: int) -> int:
    return next((s for s in THUMBNAIL_SIZES if s >= requested), THUMBNAIL_SIZES[-1])


def thumbnail_url(image_url: str, requested_size: int, image_format: str = "original") -> str:
    return f"{image_url}?size={thumbnail_size(requested_size)}&format={image_format}"
