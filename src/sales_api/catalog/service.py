from __future__ import annotations

import dataclasses
import uuid
from collections import deque
from collections.abc import Collection as CollectionT
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from ..common import rich_text
from ..common.clock import utc
from ..common.errors import DomainError, DomainException, ErrorCodes, ErrorCollector, ensure, must_exist
from ..common.slug import slugify, unique_slug
from ..events import payloads
from ..events.domain_event import EventTypes
from ..inventory.model import Stock
from ..permissions.model import Permission, Principal
from ..persistence.store import Store
from ..shop_context import ShopContext, require_permission
from . import attribute_assignment
from .attribute_assignment import AttributeValueInput
from .indexer import CatalogIndexer
from .model import (
    AssignedAttribute,
    AttributeInputType,
    AttributeScope,
    AttributeValue,
    Category,
    Collection,
    MeasurementUnit,
    MediaType,
    PreorderSettings,
    Product,
    ProductAttribute,
    ProductChannelListing,
    ProductMedia,
    ProductType,
    ProductVariant,
    PublicationListing,
    ReferenceTarget,
    VariantChannelListing,
)


@dataclass(frozen=True, kw_only=True)
class AttributeCreateInput:
    name: str
    scope: AttributeScope
    input_type: AttributeInputType
    slug: str | None = None
    values: tuple[str, ...] = ()
    unit: MeasurementUnit | None = None
    value_required: bool = False
    visible_in_storefront: bool = True
    filterable_in_storefront: bool = False
    filterable_in_dashboard: bool = False
    search_weight: int = 1
    allowed_reference_targets: tuple[ReferenceTarget, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ProductTypeCreateInput:
    name: str
    slug: str | None = None
    is_shipping_required: bool = True
    is_gift_card: bool = False
    default_weight_kg: Decimal | None = None
    tax_class_id: uuid.UUID | None = None
    product_attribute_ids: tuple[uuid.UUID, ...] = ()
    variant_attribute_ids: tuple[uuid.UUID, ...] = ()
    variant_selection_attribute_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True, kw_only=True)
class CategoryCreateInput:
    name: str
    slug: str | None = None
    parent_id: uuid.UUID | None = None
    description: dict[str, Any] | None = None


@dataclass(frozen=True, kw_only=True)
class CollectionCreateInput:
    name: str
    slug: str | None = None
    description: dict[str, Any] | None = None
    product_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ProductCreateInput:
    product_type_id: uuid.UUID
    name: str
    slug: str | None = None
    description: dict[str, Any] | None = None
    category_id: uuid.UUID | None = None
    weight_kg: Decimal | None = None
    tax_class_id: uuid.UUID | None = None
    external_reference: str | None = None
    seo_title: str | None = None
    seo_description: str | None = None
    attributes: tuple[AttributeValueInput, ...] = ()
    collection_ids: tuple[uuid.UUID, ...] = ()


@dataclass(frozen=True, kw_only=True)
class ProductUpdateInput:
    name: str | None = None
    slug: str | None = None
    description: dict[str, Any] | None = None
    category_id: uuid.UUID | None = None
    clear_category: bool = False
    weight_kg: Decimal | None = None
    rating: Decimal | None = None
    attributes: tuple[AttributeValueInput, ...] | None = None


@dataclass(frozen=True, kw_only=True)
class ProductChannelListingInput:
    channel_id: uuid.UUID
    is_published: bool = False
    published_at: datetime | None = None
    visible_in_listings: bool = True
    is_available_for_purchase: bool = False
    available_for_purchase_at: datetime | None = None


@dataclass(frozen=True, kw_only=True)
class VariantPriceInput:
    channel_id: uuid.UUID
    price: Decimal
    cost_price: Decimal | None = None
    prior_price: Decimal | None = None
    preorder_threshold: int | None = None


@dataclass(frozen=True, kw_only=True)
class VariantCreateInput:
    product_id: uuid.UUID
    sku: str | None = None
    name: str = ""
    weight_kg: Decimal | None = None
    track_inventory: bool = True
    quantity_limit_per_checkout: int | None = None
    preorder: PreorderSettings | None = None
    digital_content_url: str | None = None
    external_reference: str | None = None
    attributes: tuple[AttributeValueInput, ...] = ()
    channel_listings: tuple[VariantPriceInput, ...] = ()
    stocks: tuple[tuple[uuid.UUID, int], ...] = ()


@dataclass(frozen=True, kw_only=True)
class VariantUpdateInput:
    sku: str | None = None
    name: str | None = None
    weight_kg: Decimal | None = None
    track_inventory: bool | None = None
    quantity_limit_per_checkout: int | None = None
    clear_quantity_limit: bool = False
    preorder: PreorderSettings | None = None
    digital_content_url: str | None = None


def category_with_descendants(store: Store, category_id: uuid.UUID) -> set[uuid.UUID]:
    result = {category_id}
    frontier = deque([category_id])
    while frontier:
        current = frontier.popleft()
        for child in store.categories.values():
            if child.parent_id == current and child.id not in result:
                result.add(child.id)
                frontier.append(child.id)
    return result


class CatalogService:
    def __init__(self, ctx: ShopContext, indexer: CatalogIndexer):
        self._ctx = ctx
        self._indexer = indexer

    def create_attribute(self, principal: Principal, data: AttributeCreateInput) -> ProductAttribute:
        ctx = self._ctx
        require_permission(
            principal,
            Permission.MANAGE_PRODUCT_TYPES_AND_ATTRIBUTES
            if data.scope == AttributeScope.PRODUCT
            else Permission.MANAGE_PAGE_TYPES_AND_ATTRIBUTES,
        )

        def create(store: Store) -> ProductAttribute:
            errors = ErrorCollector()
            if not data.name or not data.name.strip():
                errors.add(ErrorCodes.REQUIRED, "name", "Name is required.")
            if data.unit is not None and data.input_type != AttributeInputType.NUMERIC:
                errors.add(ErrorCodes.INVALID, "unit", "Only numeric attributes can have a unit.")
            value_slugs = [slugify(v) for v in data.values]
            if len(set(value_slugs)) != len(value_slugs):
                errors.add(ErrorCodes.DUPLICATE, "values", "Attribute value keys must be unique.")
            errors.raise_if_any()

            attribute = ProductAttribute(
                name=data.name.strip(),
                slug=unique_slug(data.slug, data.name, lambda s: any(a.slug == s for a in store.attributes.values())),
                scope=data.scope,
                input_type=data.input_type,
                unit=data.unit,
                value_required=data.value_required,
                visible_in_storefront=data.visible_in_storefront,
                filterable_in_storefront=data.filterable_in_storefront,
                filterable_in_dashboard=data.filterable_in_dashboard,
                search_weight=data.search_weight,
            )
            attribute.allowed_reference_targets.update(data.allowed_reference_targets)
            for i, value in enumerate(data.values):
                attribute.values.append(AttributeValue(name=value, slug=value_slugs[i], sort_order=i))

            store.attributes[attribute.id] = attribute
            ctx.raise_event(EventTypes.ATTRIBUTE_CREATED, payloads.ref(attribute.id, attribute.name))
            return attribute

        return ctx.db.write(create)

    def reorder_attribute_values(
        self, principal: Principal, attribute_id: uuid.UUID, ordered_value_ids: Sequence[uuid.UUID]
    ) -> None:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_PRODUCT_TYPES_AND_ATTRIBUTES)

        def reorder(store: Store) -> None:
            attribute = must_exist(store.attributes.get(attribute_id), "attribute", attribute_id)
            ensure(
                set(ordered_value_ids) == {v.id for v in attribute.values},
                ErrorCodes.INVALID,
                "values",
                "Must list every value exactly once.",
            )
            for i, value_id in enumerate(ordered_value_ids):
                next(v for v in attribute.values if v.id == value_id).sort_order = i
            attribute.values.sort(key=lambda v: v.sort_order)
            ctx.raise_event(EventTypes.ATTRIBUTE_UPDATED, payloads.ref(attribute.id, attribute.name))

        ctx.db.write(reorder)

    def create_product_type(self, principal: Principal, data: ProductTypeCreateInput) -> ProductType:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_PRODUCT_TYPES_AND_ATTRIBUTES)

        def create(store: Store) -> ProductType:
            errors = ErrorCollector()
            for id_ in (*data.product_attribute_ids, *data.variant_attribute_ids):
                a = store.attributes.get(id_)
                if a is None or a.scope != AttributeScope.PRODUCT:
                    errors.add(ErrorCodes.INVALID, "attributes", f"Attribute {id_} is not a product attribute.")

            if set(data.product_attribute_ids) & set(data.variant_attribute_ids):
                errors.add(ErrorCodes.INVALID, "attributes", "An attribute can't be both product- and variant-level.")
            if not all(id_ in data.variant_attribute_ids for id_ in data.variant_selection_attribute_ids):
                errors.add(
                    ErrorCodes.INVALID, "variantSelectionAttributes", "Selection attributes must be variant attributes."
                )
            if data.tax_class_id is not None and data.tax_class_id not in store.tax_classes:
                errors.add(ErrorCodes.NOT_FOUND, "taxClass", "Tax class not found.")
            errors.raise_if_any()

            product_type = ProductType(
                name=data.name.strip(),
                slug=unique_slug(
                    data.slug, data.name, lambda s: any(t.slug == s for t in store.product_types.values())
                ),
                is_shipping_required=data.is_shipping_required,
                is_gift_card=data.is_gift_card,
                default_weight_kg=data.default_weight_kg,
                tax_class_id=data.tax_class_id,
            )
            product_type.product_attribute_ids.extend(data.product_attribute_ids)
            product_type.variant_attribute_ids.extend(data.variant_attribute_ids)
            product_type.variant_selection_attribute_ids.update(data.variant_selection_attribute_ids)
            store.product_types[product_type.id] = product_type
            ctx.raise_event(EventTypes.PRODUCT_TYPE_CREATED, payloads.ref(product_type.id, product_type.name))
            return product_type

        return ctx.db.write(create)

    def unassign_attributes(
        self, principal: Principal, product_type_id: uuid.UUID, attribute_ids: Sequence[uuid.UUID]
    ) -> None:
        require_permission(principal, Permission.MANAGE_PRODUCT_TYPES_AND_ATTRIBUTES)

        def unassign(store: Store) -> None:
            product_type = must_exist(store.product_types.get(product_type_id), "productType", product_type_id)
            product_type.product_attribute_ids[:] = [
                a for a in product_type.product_attribute_ids if a not in attribute_ids
            ]
            product_type.variant_attribute_ids[:] = [
                a for a in product_type.variant_attribute_ids if a not in attribute_ids
            ]
            product_type.variant_selection_attribute_ids.difference_update(attribute_ids)
            for product in store.products.values():
                if product.product_type_id == product_type_id:
                    self._indexer.schedule_product(product.id)

        self._ctx.db.write(unassign)

    def create_category(self, principal: Principal, data: CategoryCreateInput) -> Category:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_PRODUCTS)

        def create(store: Store) -> Category:
            if data.parent_id is not None:
                must_exist(store.categories.get(data.parent_id), "parent", data.parent_id)
            category = Category(
                name=data.name.strip(),
                parent_id=data.parent_id,
                slug=unique_slug(data.slug, data.name, lambda s: any(c.slug == s for c in store.categories.values())),
                description=None if data.description is None else rich_text.sanitize(data.description),
            )
            store.categories[category.id] = category
            ctx.raise_event(EventTypes.CATEGORY_CREATED, payloads.ref(category.id, category.name))
            return category

        return ctx.db.write(create)

    def delete_category(self, principal: Principal, category_id: uuid.UUID) -> None:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_PRODUCTS)

        def delete(store: Store) -> None:
            must_exist(store.categories.get(category_id), "category", category_id)
            subtree = category_with_descendants(store, category_id)
            for product in store.products.values():
                if product.category_id is None or product.category_id not in subtree:
                    continue
                product.category_id = None
                for channel_id, listing in list(product.channel_listings.items()):
                    product.channel_listings[channel_id] = dataclasses.replace(
                        listing, is_published=False, published_at=None
                    )
                product.updated_at = ctx.now
                self._indexer.schedule_product(product.id)
                ctx.raise_event(EventTypes.PRODUCT_UPDATED, payloads.product(store, product))

            for id_ in subtree:
                category = store.categories.pop(id_)
                ctx.raise_event(EventTypes.CATEGORY_DELETED, payloads.ref(id_, category.name))

        ctx.db.write(delete)

    def create_collection(self, principal: Principal, data: CollectionCreateInput) -> Collection:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_PRODUCTS)

        def create(store: Store) -> Collection:
            for pid in data.product_ids:
                must_exist(store.products.get(pid), "products", pid)
            collection = Collection(
                name=data.name.strip(),
                slug=unique_slug(data.slug, data.name, lambda s: any(c.slug == s for c in store.collections.values())),
                description=None if data.description is None else rich_text.sanitize(data.description),
            )
            collection.product_ids.extend(dict.fromkeys(data.product_ids))
            store.collections[collection.id] = collection
            for pid in collection.product_ids:
                self._indexer.schedule_product(pid)
            ctx.raise_event(EventTypes.COLLECTION_CREATED, payloads.ref(collection.id, collection.name))
            return collection

        return ctx.db.write(create)

    def set_collection_products(
        self, principal: Principal, collection_id: uuid.UUID, ordered_product_ids: Sequence[uuid.UUID]
    ) -> None:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_PRODUCTS)

        def update(store: Store) -> None:
            collection = must_exist(store.collections.get(collection_id), "collection", collection_id)
            for pid in ordered_product_ids:
                must_exist(store.products.get(pid), "products", pid)
            affected = list(dict.fromkeys([*collection.product_ids, *ordered_product_ids]))
            collection.product_ids[:] = list(dict.fromkeys(ordered_product_ids))
            for pid in affected:
                self._indexer.schedule_product(pid)
            ctx.raise_event(EventTypes.COLLECTION_UPDATED, payloads.ref(collection.id, collection.name))

        ctx.db.write(update)

    def set_collection_channel_listing(
        self,
        principal: Principal,
        collection_id: uuid.UUID,
        channel_id: uuid.UUID,
        is_published: bool,
        published_at: datetime | None = None,
    ) -> None:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_PRODUCTS)

        def update(store: Store) -> None:
            collection = must_exist(store.collections.get(collection_id), "collection", collection_id)
            must_exist(store.channels.get(channel_id), "channel", channel_id)
            collection.channel_listings[channel_id] = PublicationListing(
                channel_id, is_published, utc(published_at) if published_at is not None else None
            )
            ctx.raise_event(EventTypes.COLLECTION_UPDATED, payloads.ref(collection.id, collection.name))

        ctx.db.write(update)

    def create_product(self, principal: Principal, data: ProductCreateInput) -> Product:
        return self._ctx.db.write(lambda store: self.create_product_tx(principal, store, data))

    # *_tx: caller must already be inside db.write (used by BulkService)

    def create_product_tx(self, principal: Principal, store: Store, data: ProductCreateInput) -> Product:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_PRODUCTS)
        errors = ErrorCollector()
        product_type = store.product_types.get(data.product_type_id)
        if product_type is None:
            raise DomainException(ErrorCodes.NOT_FOUND, "productType", "Product type not found.")
        if not data.name or not data.name.strip():
            errors.add(ErrorCodes.REQUIRED, "name", "Name is required.")
        if data.category_id is not None and data.category_id not in store.categories:
            errors.add(ErrorCodes.NOT_FOUND, "category", "Category not found.")
        if data.weight_kg is not None and data.weight_kg < 0:
            errors.add(ErrorCodes.INVALID, "weight", "Weight cannot be negative.")
        for cid in data.collection_ids:
            if cid not in store.collections:
                errors.add(ErrorCodes.NOT_FOUND, "collections", f"Collection {cid} not found.")
        errors.raise_if_any()

        attributes = attribute_assignment.resolve(
            store, product_type.product_attribute_ids, data.attributes, True, "attributes", errors
        )
        errors.raise_if_any()

        product = Product(
            product_type_id=product_type.id,
            name=data.name.strip(),
            slug=unique_slug(data.slug, data.name, lambda s: store.products.find_by_slug(s) is not None),
            description=None if data.description is None else rich_text.sanitize(data.description),
            category_id=data.category_id,
            weight_kg=data.weight_kg,
            tax_class_id=data.tax_class_id,
            external_reference=data.external_reference,
            seo_title=data.seo_title,
            seo_description=data.seo_description,
            created_at=ctx.now,
            updated_at=ctx.now,
        )
        product.attributes.extend(attributes)
        store.products.add(product.id, product)
        for cid in data.collection_ids:
            store.collections[cid].product_ids.append(product.id)
        self._indexer.schedule_product(product.id)
        ctx.raise_event(EventTypes.PRODUCT_CREATED, payloads.product(store, product))
        return product

    def update_product(self, principal: Principal, product_id: uuid.UUID, data: ProductUpdateInput) -> Product:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_PRODUCTS)

        def update(store: Store) -> Product:
            product = must_exist(store.products.get(product_id), "product", product_id)
            product_type = store.product_types[product.product_type_id]
            errors = ErrorCollector()
            if data.category_id is not None and data.category_id not in store.categories:
                errors.add(ErrorCodes.NOT_FOUND, "category", "Category not found.")
            attributes = (
                None
                if data.attributes is None
                else attribute_assignment.resolve(
                    store, product_type.product_attribute_ids, data.attributes, False, "attributes", errors
                )
            )
            if data.clear_category and any(listing.is_published for listing in product.channel_listings.values()):
                errors.add(
                    ErrorCodes.CATEGORY_MISSING,
                    "category",
                    "A published product must have a category.",
                )
            errors.raise_if_any()

            if data.name is not None:
                product.name = data.name.strip()
            if data.slug is not None and data.slug != product.slug:

                def taken(s: str) -> bool:
                    other = store.products.find_by_slug(s)
                    return other is not None and other.id != product_id

                store.products.set_slug(product, unique_slug(data.slug, product.name, taken))
            if data.description is not None:
                product.description = rich_text.sanitize(data.description)
            if data.category_id is not None:
                product.category_id = data.category_id
            if data.clear_category:
                product.category_id = None
            if data.weight_kg is not None:
                product.weight_kg = data.weight_kg
            if data.rating is not None:
                product.rating = data.rating
            if attributes is not None:
                for a in attributes:
                    product.attributes[:] = [x for x in product.attributes if x.attribute_id != a.attribute_id]
                    product.attributes.append(a)

            product.updated_at = ctx.now
            self._indexer.schedule_product(product.id)
            ctx.raise_event(EventTypes.PRODUCT_UPDATED, payloads.product(store, product))
            return product

        return ctx.db.write(update)

    def update_product_channel_listings(
        self,
        principal: Principal,
        product_id: uuid.UUID,
        listings: Sequence[ProductChannelListingInput],
        remove_channels: Sequence[uuid.UUID] | None = None,
    ) -> None:
        self._ctx.db.write(
            lambda store: self.update_product_channel_listings_tx(
                principal, store, product_id, listings, remove_channels
            )
        )

    def update_product_channel_listings_tx(
        self,
        principal: Principal,
        store: Store,
        product_id: uuid.UUID,
        listings: Sequence[ProductChannelListingInput],
        remove_channels: Sequence[uuid.UUID] | None,
    ) -> None:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_PRODUCTS)
        product = must_exist(store.products.get(product_id), "product", product_id)
        errors = ErrorCollector()
        for listing in listings:
            if listing.channel_id not in store.channels:
                errors.add(ErrorCodes.NOT_FOUND, "channelId", f"Channel {listing.channel_id} not found.")
            if listing.is_published and product.category_id is None:
                errors.add(
                    ErrorCodes.CATEGORY_MISSING,
                    "isPublished",
                    "You must select a category to be able to publish.",
                )

        errors.raise_if_any()

        for channel_id in remove_channels or ():
            product.channel_listings.pop(channel_id, None)
            for variant in store.variants.of_product(product_id):
                variant.channel_listings.pop(channel_id, None)

        for item in listings:
            product.channel_listings[item.channel_id] = ProductChannelListing(
                channel_id=item.channel_id,
                is_published=item.is_published,
                published_at=utc(item.published_at or ctx.now) if item.is_published else None,
                visible_in_listings=item.visible_in_listings,
                is_available_for_purchase=item.is_available_for_purchase,
                available_for_purchase_at=utc(item.available_for_purchase_at or ctx.now)
                if item.is_available_for_purchase
                else None,
            )

        product.updated_at = ctx.now
        self._indexer.schedule_product(product.id)
        ctx.raise_event(EventTypes.PRODUCT_UPDATED, payloads.product(store, product))

    def delete_product(self, principal: Principal, product_id: uuid.UUID) -> None:
        self._ctx.db.write(lambda store: self.delete_product_tx(principal, store, product_id))

    def delete_product_tx(self, principal: Principal, store: Store, product_id: uuid.UUID) -> None:
        require_permission(principal, Permission.MANAGE_PRODUCTS)
        product = must_exist(store.products.get(product_id), "product", product_id)
        for variant in store.variants.of_product(product_id):
            self._delete_variant_tx(store, variant)
        for collection in store.collections.values():
            if product_id in collection.product_ids:
                collection.product_ids.remove(product_id)
        store.products.remove(product_id)
        self._indexer.remove(product_id)
        self._ctx.raise_event(EventTypes.PRODUCT_DELETED, payloads.ref(product.id, product.name))

    def create_variant(self, principal: Principal, data: VariantCreateInput) -> ProductVariant:
        return self._ctx.db.write(lambda store: self.create_variant_tx(principal, store, data))

    def validate_variant(
        self, store: Store, data: VariantCreateInput, reserved_skus: CollectionT[str] | None = None
    ) -> list[DomainError]:
        errors = ErrorCollector()
        product = store.products.get(data.product_id)
        if product is None:
            return errors.add(ErrorCodes.NOT_FOUND, "product", "Product not found.").errors
        product_type = store.product_types[product.product_type_id]

        if data.sku is not None:
            normalized = data.sku.strip()
            reserved = {s.upper() for s in reserved_skus or ()}
            if store.variants.find_by_sku(normalized) is not None or normalized.upper() in reserved:
                errors.add(ErrorCodes.DUPLICATE, "sku", f"SKU '{normalized}' already exists.")

        existing = len(store.variants.of_product(product.id))
        if not product_type.has_variants and existing > 0:
            errors.add(
                ErrorCodes.INVALID,
                "attributes",
                "Products of a type without variant attributes have exactly one variant.",
            )

        if data.quantity_limit_per_checkout is not None and data.quantity_limit_per_checkout <= 0:
            errors.add(ErrorCodes.INVALID, "quantityLimitPerCustomer", "Limit must be positive.")
        for listing in data.channel_listings:
            if listing.channel_id not in store.channels:
                errors.add(ErrorCodes.NOT_FOUND, "channelListings", f"Channel {listing.channel_id} not found.")
            if listing.price < 0 or (listing.cost_price is not None and listing.cost_price < 0):
                errors.add(ErrorCodes.INVALID, "price", "Price cannot be negative.")

        for warehouse_id, quantity in data.stocks:
            if warehouse_id not in store.warehouses:
                errors.add(ErrorCodes.NOT_FOUND, "stocks", f"Warehouse {warehouse_id} not found.")
            if quantity < 0:
                errors.add(ErrorCodes.INVALID, "stocks", "Quantity cannot be negative.")

        return errors.errors

    def create_variant_tx(self, principal: Principal, store: Store, data: VariantCreateInput) -> ProductVariant:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_PRODUCTS)
        errors = ErrorCollector()
        for e in self.validate_variant(store, data):
            errors.add(e)
        errors.raise_if_any()

        product = store.products[data.product_id]
        product_type = store.product_types[product.product_type_id]
        attributes = attribute_assignment.resolve(
            store, product_type.variant_attribute_ids, data.attributes, True, "attributes", errors
        )
        errors.raise_if_any()
        _ensure_unique_combination(store, product.id, None, attributes, product_type)

        variant = ProductVariant(
            product_id=product.id,
            sku=data.sku.strip() if data.sku is not None else None,
            name=data.name,
            weight_kg=data.weight_kg,
            track_inventory=data.track_inventory,
            quantity_limit_per_checkout=data.quantity_limit_per_checkout,
            preorder=data.preorder,
            digital_content_url=data.digital_content_url,
            external_reference=data.external_reference,
            sort_order=len(store.variants.of_product(product.id)),
            created_at=ctx.now,
            updated_at=ctx.now,
        )
        variant.attributes.extend(attributes)
        for listing in data.channel_listings:
            variant.channel_listings[listing.channel_id] = VariantChannelListing(
                channel_id=listing.channel_id,
                price=listing.price,
                cost_price=listing.cost_price,
                prior_price=listing.prior_price,
                preorder_quantity_threshold=listing.preorder_threshold,
                discounted_price=listing.price,
            )

        store.variants.add(variant.id, variant)
        for warehouse_id, quantity in data.stocks:
            store.stocks[(warehouse_id, variant.id)] = Stock(
                warehouse_id=warehouse_id, variant_id=variant.id, quantity_in_stock=quantity
            )

        product.updated_at = ctx.now
        self._indexer.schedule_product(product.id)
        ctx.raise_event(EventTypes.PRODUCT_VARIANT_CREATED, payloads.variant(store, variant))
        return variant

    def update_variant(self, principal: Principal, variant_id: uuid.UUID, data: VariantUpdateInput) -> ProductVariant:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_PRODUCTS)

        def update(store: Store) -> ProductVariant:
            variant = must_exist(store.variants.get(variant_id), "variant", variant_id)
            if data.sku is not None:
                other = store.variants.find_by_sku(data.sku.strip())
                ensure(
                    other is None or other.id == variant_id,
                    ErrorCodes.DUPLICATE,
                    "sku",
                    f"SKU '{data.sku}' already exists.",
                )
                store.variants.set_sku(variant, data.sku.strip())

            ensure(
                data.quantity_limit_per_checkout is None or data.quantity_limit_per_checkout > 0,
                ErrorCodes.INVALID,
                "quantityLimitPerCustomer",
                "Limit must be positive.",
            )
            if data.name is not None:
                variant.name = data.name
            if data.weight_kg is not None:
                variant.weight_kg = data.weight_kg
            if data.track_inventory is not None:
                variant.track_inventory = data.track_inventory
            if data.quantity_limit_per_checkout is not None:
                variant.quantity_limit_per_checkout = data.quantity_limit_per_checkout
            if data.clear_quantity_limit:
                variant.quantity_limit_per_checkout = None
            if data.preorder is not None:
                variant.preorder = data.preorder
            if data.digital_content_url is not None:
                variant.digital_content_url = data.digital_content_url
            variant.updated_at = ctx.now
            self._indexer.schedule_product(variant.product_id)
            ctx.raise_event(EventTypes.PRODUCT_VARIANT_UPDATED, payloads.variant(store, variant))
            return variant

        return ctx.db.write(update)

    def update_variant_prices(
        self, principal: Principal, variant_id: uuid.UUID, prices: Sequence[VariantPriceInput]
    ) -> None:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_PRODUCTS)

        def update(store: Store) -> None:
            variant = must_exist(store.variants.get(variant_id), "variant", variant_id)
            for p in prices:
                must_exist(store.channels.get(p.channel_id), "channelId", p.channel_id)
                ensure(
                    p.price >= 0 and (p.cost_price is None or p.cost_price >= 0),
                    ErrorCodes.INVALID,
                    "price",
                    "Price cannot be negative.",
                )

            for p in prices:
                previous = variant.channel_listings.get(p.channel_id)
                variant.channel_listings[p.channel_id] = VariantChannelListing(
                    channel_id=p.channel_id,
                    price=p.price,
                    cost_price=p.cost_price,
                    prior_price=p.prior_price,
                    preorder_quantity_threshold=p.preorder_threshold,
                    discounted_price=previous.discounted_price
                    if previous is not None and previous.discounted_price is not None
                    else p.price,
                )

            variant.updated_at = ctx.now
            self._indexer.schedule_product(variant.product_id)
            ctx.raise_event(EventTypes.PRODUCT_VARIANT_UPDATED, payloads.variant(store, variant))

        ctx.db.write(update)

    def reorder_variants(
        self, principal: Principal, product_id: uuid.UUID, ordered_variant_ids: Sequence[uuid.UUID]
    ) -> None:
        require_permission(principal, Permission.MANAGE_PRODUCTS)

        def reorder(store: Store) -> None:
            variants = store.variants.of_product(product_id)
            ensure(
                set(ordered_variant_ids) == {v.id for v in variants},
                ErrorCodes.INVALID,
                "moves",
                "Must list every variant of the product.",
            )
            for i, variant_id in enumerate(ordered_variant_ids):
                store.variants[variant_id].sort_order = i

        self._ctx.db.write(reorder)

    def delete_variant(self, principal: Principal, variant_id: uuid.UUID) -> None:
        require_permission(principal, Permission.MANAGE_PRODUCTS)
        self._ctx.db.write(
            lambda store: self._delete_variant_tx(
                store, must_exist(store.variants.get(variant_id), "variant", variant_id)
            )
        )

    def _delete_variant_tx(self, store: Store, variant: ProductVariant) -> None:
        for warehouse_id in store.warehouses:
            store.stocks.pop((warehouse_id, variant.id), None)
        store.reservations.remove_all(lambda r: r.variant_id == variant.id)
        for checkout in store.checkouts.values():
            checkout.lines[:] = [line for line in checkout.lines if line.variant_id != variant.id]
        store.variants.remove(variant.id)
        self._indexer.schedule_product(variant.product_id)
        self._ctx.raise_event(EventTypes.PRODUCT_VARIANT_DELETED, payloads.ref(variant.id, variant.name))

    def add_image(self, principal: Principal, product_id: uuid.UUID, url: str, alt: str = "") -> ProductMedia:
        require_permission(principal, Permission.MANAGE_PRODUCTS)

        def add(store: Store) -> ProductMedia:
            product = must_exist(store.products.get(product_id), "product", product_id)
            media = ProductMedia(type=MediaType.IMAGE, url=url, alt=alt, sort_order=len(product.media))
            product.media.append(media)
            return media

        return self._ctx.db.write(add)


def _ensure_unique_combination(
    store: Store,
    product_id: uuid.UUID,
    except_variant_id: uuid.UUID | None,
    attributes: Sequence[AssignedAttribute],
    kind: ProductType,
) -> None:
    if not kind.has_variants:
        return
    key = attribute_assignment.combination_key(attributes)
    duplicate = any(
        v.id != except_variant_id and attribute_assignment.combination_key(v.attributes) == key
        for v in store.variants.of_product(product_id)
    )
    if duplicate:
        raise DomainException(
            ErrorCodes.VARIANT_DUPLICATE,
            "attributes",
            "A variant with this combination of attribute values already exists.",
        )
