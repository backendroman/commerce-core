from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import TYPE_CHECKING

from ..accounts.model import normalize_email
from ..common.case_insensitive import CaseInsensitiveDict
from ..settings import ShopSettings
from .table import (
    CheckoutTable,
    GiftCardTable,
    OrderTable,
    PreorderAllocationSet,
    ProductTable,
    ReservationSet,
    UserTable,
    VariantTable,
    VoucherTable,
)

if TYPE_CHECKING:
    from ..accounts.model import User
    from ..apps.model import App, WebhookDelivery, WebhookSubscription
    from ..catalog.model import Category, Collection, ProductAttribute, ProductType
    from ..channels.model import Channel
    from ..checkout.model import Checkout
    from ..discounts.model import GiftCard, Promotion, Voucher, VoucherCode
    from ..inventory.model import Allocation, Stock, Warehouse
    from ..payments.model import LegacyPayment, Transaction
    from ..permissions.model import PermissionGroup
    from ..shipping.model import ShippingMethod, ShippingZone
    from ..taxes.model import CountryTaxRates, TaxClass


class Store:
    def __init__(self) -> None:
        self.settings = ShopSettings()

        self.channels: dict[uuid.UUID, Channel] = {}

        self.attributes: dict[uuid.UUID, ProductAttribute] = {}
        self.product_types: dict[uuid.UUID, ProductType] = {}
        self.categories: dict[uuid.UUID, Category] = {}
        self.collections: dict[uuid.UUID, Collection] = {}
        self.products = ProductTable()
        self.variants = VariantTable()

        self.warehouses: dict[uuid.UUID, Warehouse] = {}
        self.stocks: dict[tuple[uuid.UUID, uuid.UUID], Stock] = {}
        self.allocations: list[Allocation] = []
        self.preorder_allocations = PreorderAllocationSet()
        self.reservations = ReservationSet()

        self.shipping_zones: dict[uuid.UUID, ShippingZone] = {}

        self.tax_classes: dict[uuid.UUID, TaxClass] = {}
        self.tax_rates: CaseInsensitiveDict[CountryTaxRates] = CaseInsensitiveDict()

        self.promotions: dict[uuid.UUID, Promotion] = {}
        self.vouchers = VoucherTable()
        self.gift_cards = GiftCardTable()

        self.checkouts = CheckoutTable()
        self.transactions: dict[uuid.UUID, Transaction] = {}
        self.legacy_payments: dict[uuid.UUID, LegacyPayment] = {}
        self.orders = OrderTable()

        self.users = UserTable()
        self.permission_groups: dict[uuid.UUID, PermissionGroup] = {}

        self.apps: dict[uuid.UUID, App] = {}
        self.subscriptions: dict[uuid.UUID, WebhookSubscription] = {}
        self.deliveries: dict[uuid.UUID, WebhookDelivery] = {}

        self._last_order_number = 0
        self._shipping_method_cache: dict[uuid.UUID, ShippingMethod] = {}

    def next_order_number(self) -> int:
        self._last_order_number += 1
        return self._last_order_number

    @property
    def shipping_methods(self) -> Iterator[ShippingMethod]:
        return (m for z in list(self.shipping_zones.values()) for m in z.methods)

    def find_shipping_method(self, id_: uuid.UUID | None) -> ShippingMethod | None:
        if id_ is None:
            return None
        # Zones are edited in place, so the cached hit has to be verified.
        cached = self._shipping_method_cache.get(id_)
        if cached is not None:
            zone = self.shipping_zones.get(cached.shipping_zone_id)
            if zone is not None and any(m is cached for m in zone.methods):
                return cached
        method = next((m for m in self.shipping_methods if m.id == id_), None)
        if method is None:
            self._shipping_method_cache.pop(id_, None)
        else:
            self._shipping_method_cache[id_] = method
        return method

    def find_checkout_by_token(self, token: uuid.UUID) -> Checkout | None:
        return self.checkouts.find_by_token(token)

    def find_user_by_email(self, email: str) -> User | None:
        return self.users.find_by_normalized_email(normalize_email(email))

    def find_voucher_by_code(self, code: str) -> tuple[Voucher | None, VoucherCode | None]:
        return self.vouchers.find_by_code(code.strip().upper())

    def find_gift_card_by_code(self, code: str) -> GiftCard | None:
        return self.gift_cards.find_by_code(code.strip().upper())

    def find_stock(self, warehouse_id: uuid.UUID, variant_id: uuid.UUID) -> Stock | None:
        return self.stocks.get((warehouse_id, variant_id))

    def stocks_of_variant(self, variant_id: uuid.UUID) -> list[Stock]:
        result = []
        for w in self.warehouses:
            stock = self.stocks.get((w, variant_id))
            if stock is not None:
                result.append(stock)
        return result
