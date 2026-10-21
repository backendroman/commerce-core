from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum, auto
from typing import Any

from ..common.case_insensitive import CaseInsensitiveDict


class AttributeScope(StrEnum):
    PRODUCT = auto()
    CONTENT = auto()


class AttributeInputType(StrEnum):
    DROPDOWN = auto()
    MULTISELECT = auto()
    PLAIN_TEXT = auto()
    RICH_TEXT = auto()
    NUMERIC = auto()
    BOOLEAN = auto()
    DATE = auto()
    DATE_TIME = auto()
    FILE = auto()
    REFERENCE = auto()
    SWATCH = auto()


class ReferenceTarget(StrEnum):
    PRODUCT = auto()
    VARIANT = auto()
    PAGE = auto()
    CATEGORY = auto()
    COLLECTION = auto()


class MeasurementUnit(StrEnum):
    MM = auto()
    CM = auto()
    DM = auto()
    M = auto()
    KM = auto()
    FT = auto()
    YD = auto()
    INCH = auto()
    SQ_MM = auto()
    SQ_CM = auto()
    SQ_M = auto()
    SQ_KM = auto()
    SQ_FT = auto()
    SQ_INCH = auto()
    CUBIC_MM = auto()
    CUBIC_CM = auto()
    CUBIC_M = auto()
    ML = auto()
    L = auto()
    FL_OZ = auto()
    QT = auto()
    PINT = auto()
    GALLON = auto()
    G = auto()
    LB = auto()
    OZ = auto()
    KG = auto()
    TONNE = auto()
    CELSIUS = auto()
    FAHRENHEIT = auto()
    WATT = auto()
    VOLT = auto()
    AMPERE = auto()
    HERTZ = auto()
    SECOND = auto()
    MINUTE = auto()
    HOUR = auto()


@dataclass(eq=False, kw_only=True)
class AttributeValue:
    name: str
    slug: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    sort_order: int = 0
    swatch_color: str | None = None
    swatch_image_url: str | None = None
    translations: CaseInsensitiveDict[str] = field(default_factory=CaseInsensitiveDict)


@dataclass(eq=False, kw_only=True)
class ProductAttribute:
    name: str
    slug: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    scope: AttributeScope = AttributeScope.PRODUCT
    input_type: AttributeInputType = AttributeInputType.DROPDOWN
    unit: MeasurementUnit | None = None
    allowed_reference_targets: set[ReferenceTarget] = field(default_factory=set)

    value_required: bool = False
    visible_in_storefront: bool = True
    filterable_in_storefront: bool = False
    filterable_in_dashboard: bool = False
    search_weight: int = 1

    values: list[AttributeValue] = field(default_factory=list)
    translations: CaseInsensitiveDict[str] = field(default_factory=CaseInsensitiveDict)
    metadata: dict[str, str] = field(default_factory=dict)
    private_metadata: dict[str, str] = field(default_factory=dict)

    @property
    def has_predefined_values(self) -> bool:
        return self.input_type in (
            AttributeInputType.DROPDOWN,
            AttributeInputType.MULTISELECT,
            AttributeInputType.SWATCH,
        )


@dataclass(frozen=True, kw_only=True)
class AssignedAttribute:
    attribute_id: uuid.UUID
    value_ids: tuple[uuid.UUID, ...] = ()
    text: str | None = None
    rich_text: dict[str, Any] | None = None
    number: Decimal | None = None
    boolean: bool | None = None
    date: date | None = None
    date_time: datetime | None = None
    file_url: str | None = None
    references: tuple[uuid.UUID, ...] = ()


@dataclass(eq=False, kw_only=True)
class ProductType:
    name: str
    slug: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    is_shipping_required: bool = True
    is_gift_card: bool = False
    default_weight_kg: Decimal | None = None
    tax_class_id: uuid.UUID | None = None
    product_attribute_ids: list[uuid.UUID] = field(default_factory=list)
    variant_attribute_ids: list[uuid.UUID] = field(default_factory=list)
    variant_selection_attribute_ids: set[uuid.UUID] = field(default_factory=set)
    metadata: dict[str, str] = field(default_factory=dict)
    private_metadata: dict[str, str] = field(default_factory=dict)

    @property
    def has_variants(self) -> bool:
        return len(self.variant_attribute_ids) > 0


@dataclass(eq=False, kw_only=True)
class Category:
    name: str
    slug: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    parent_id: uuid.UUID | None = None
    description: dict[str, Any] | None = None
    background_image_url: str | None = None
    seo_title: str | None = None
    seo_description: str | None = None
    translations: CaseInsensitiveDict[str] = field(default_factory=CaseInsensitiveDict)
    metadata: dict[str, str] = field(default_factory=dict)
    private_metadata: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PublicationListing:
    channel_id: uuid.UUID
    is_published: bool
    published_at: datetime | None


@dataclass(eq=False, kw_only=True)
class Collection:
    name: str
    slug: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    description: dict[str, Any] | None = None
    background_image_url: str | None = None
    seo_title: str | None = None
    seo_description: str | None = None
    product_ids: list[uuid.UUID] = field(default_factory=list)
    channel_listings: dict[uuid.UUID, PublicationListing] = field(default_factory=dict)
    translations: CaseInsensitiveDict[str] = field(default_factory=CaseInsensitiveDict)
    metadata: dict[str, str] = field(default_factory=dict)
    private_metadata: dict[str, str] = field(default_factory=dict)

    def is_visible_in(self, channel_id: uuid.UUID, now: datetime) -> bool:
        listing = self.channel_listings.get(channel_id)
        return (
            listing is not None
            and listing.is_published
            and (listing.published_at is None or listing.published_at <= now)
        )


@dataclass(frozen=True, kw_only=True)
class ProductChannelListing:
    channel_id: uuid.UUID
    is_published: bool = False
    published_at: datetime | None = None
    visible_in_listings: bool = True
    is_available_for_purchase: bool = False
    available_for_purchase_at: datetime | None = None

    def is_visible_at(self, now: datetime) -> bool:
        return self.is_published and (self.published_at is None or self.published_at <= now)

    def is_purchasable_at(self, now: datetime) -> bool:
        return (
            self.is_visible_at(now)
            and self.is_available_for_purchase
            and (self.available_for_purchase_at is None or self.available_for_purchase_at <= now)
        )


class MediaType(StrEnum):
    IMAGE = auto()
    VIDEO = auto()


@dataclass(eq=False, kw_only=True)
class ProductMedia:
    url: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    type: MediaType = MediaType.IMAGE
    alt: str = ""
    sort_order: int = 0
    focal_point: tuple[Decimal, Decimal] | None = None
    oembed_html: str | None = None
    oembed_thumbnail_url: str | None = None


@dataclass(eq=False, kw_only=True)
class Product:
    product_type_id: uuid.UUID
    name: str
    slug: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    description: dict[str, Any] | None = None
    seo_title: str | None = None
    seo_description: str | None = None
    category_id: uuid.UUID | None = None
    weight_kg: Decimal | None = None
    tax_class_id: uuid.UUID | None = None
    rating: Decimal | None = None
    external_reference: str | None = None
    attributes: list[AssignedAttribute] = field(default_factory=list)
    media: list[ProductMedia] = field(default_factory=list)
    channel_listings: dict[uuid.UUID, ProductChannelListing] = field(default_factory=dict)
    translations: CaseInsensitiveDict[str] = field(default_factory=CaseInsensitiveDict)
    metadata: dict[str, str] = field(default_factory=dict)
    private_metadata: dict[str, str] = field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, kw_only=True)
class VariantChannelListing:
    channel_id: uuid.UUID
    price: Decimal | None = None
    cost_price: Decimal | None = None
    prior_price: Decimal | None = None
    preorder_quantity_threshold: int | None = None

    discounted_price: Decimal | None = None


@dataclass(frozen=True)
class PreorderSettings:
    global_threshold: int | None = None
    end_date: datetime | None = None

    def is_active_at(self, now: datetime) -> bool:
        return self.end_date is None or self.end_date > now


@dataclass(eq=False, kw_only=True)
class ProductVariant:
    product_id: uuid.UUID
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    sku: str | None = None
    name: str = ""
    weight_kg: Decimal | None = None
    track_inventory: bool = True
    quantity_limit_per_checkout: int | None = None
    preorder: PreorderSettings | None = None
    preorder_migration_failure_notified_at: datetime | None = None
    digital_content_url: str | None = None
    external_reference: str | None = None
    sort_order: int = 0
    attributes: list[AssignedAttribute] = field(default_factory=list)
    media_ids: list[uuid.UUID] = field(default_factory=list)
    channel_listings: dict[uuid.UUID, VariantChannelListing] = field(default_factory=dict)
    translations: CaseInsensitiveDict[str] = field(default_factory=CaseInsensitiveDict)
    metadata: dict[str, str] = field(default_factory=dict)
    private_metadata: dict[str, str] = field(default_factory=dict)
    created_at: datetime
    updated_at: datetime

    @property
    def is_digital(self) -> bool:
        return self.digital_content_url is not None

    def is_in_preorder_at(self, now: datetime) -> bool:
        return self.preorder is not None and self.preorder.is_active_at(now)
