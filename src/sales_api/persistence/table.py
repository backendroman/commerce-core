from __future__ import annotations

import uuid
from collections.abc import Callable, Hashable, Iterable, Iterator
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..accounts.model import User
    from ..catalog.model import Product, ProductVariant
    from ..checkout.model import Checkout
    from ..discounts.model import GiftCard, Voucher, VoucherCode
    from ..inventory.model import PreorderAllocation, Reservation
    from ..orders.model import Order


class Table[K: Hashable, V]:
    def __init__(self) -> None:
        self._items: dict[K, V] = {}
        self.removed: list[Callable[[V], None]] = []

    def __len__(self) -> int:
        return len(self._items)

    def __getitem__(self, key: K) -> V:
        return self._items[key]

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def __iter__(self) -> Iterator[K]:
        return iter(self._items)

    def keys(self):
        return self._items.keys()

    def values(self):
        return self._items.values()

    def items(self):
        return self._items.items()

    def get(self, key: K | None, default: V | None = None) -> V | None:
        return self._items.get(key, default) if key is not None else default

    def add(self, key: K, value: V) -> None:
        if key in self._items:
            raise KeyError(f"duplicate key: {key!r}")
        self._items[key] = value
        self._on_added(value)

    def add_if_missing(self, key: K, value: V) -> bool:
        if key in self._items:
            return False
        self._items[key] = value
        self._on_added(value)
        return True

    def remove(self, key: K) -> bool:
        value = self._items.pop(key, None)
        if value is None:
            return False
        self._on_removed(value)
        for handler in list(self.removed):
            handler(value)
        return True

    def _on_added(self, value: V) -> None:
        pass

    def _on_removed(self, value: V) -> None:
        pass


class MultiIndex[K: Hashable, V]:
    def __init__(self) -> None:
        self._groups: dict[K, list[V]] = {}

    def get(self, key: K) -> list[V]:
        return self._groups.get(key, [])

    def add(self, key: K, value: V) -> None:
        self._groups.setdefault(key, []).append(value)

    def remove(self, key: K, value: V) -> None:
        group = self._groups.get(key)
        if group is None:
            return
        for i, item in enumerate(group):
            if item is value:
                del group[i]
                break
        if not group:
            del self._groups[key]

    def take(self, key: K) -> list[V] | None:
        return self._groups.pop(key, None)


class VariantTable(Table[uuid.UUID, "ProductVariant"]):
    def __init__(self) -> None:
        super().__init__()
        self._by_product: MultiIndex[uuid.UUID, ProductVariant] = MultiIndex()
        self._by_sku: dict[str, ProductVariant] = {}  # keys are upper()-ed

    def of_product(self, product_id: uuid.UUID) -> list[ProductVariant]:
        return list(self._by_product.get(product_id))

    def find_by_sku(self, sku: str) -> ProductVariant | None:
        return self._by_sku.get(sku.upper())

    def set_sku(self, variant: ProductVariant, sku: str | None) -> None:
        self._unindex_sku(variant)
        variant.sku = sku
        if sku is not None:
            self._by_sku[sku.upper()] = variant

    def _on_added(self, value: ProductVariant) -> None:
        self._by_product.add(value.product_id, value)
        if value.sku is not None:
            self._by_sku[value.sku.upper()] = value

    def _on_removed(self, value: ProductVariant) -> None:
        self._by_product.remove(value.product_id, value)
        self._unindex_sku(value)

    def _unindex_sku(self, variant: ProductVariant) -> None:
        if variant.sku is not None and self._by_sku.get(variant.sku.upper()) is variant:
            del self._by_sku[variant.sku.upper()]


class ProductTable(Table[uuid.UUID, "Product"]):
    def __init__(self) -> None:
        super().__init__()
        self._by_slug: dict[str, Product] = {}

    def find_by_slug(self, slug: str) -> Product | None:
        return self._by_slug.get(slug)

    def set_slug(self, product: Product, slug: str) -> None:
        if self._by_slug.get(product.slug) is product:
            del self._by_slug[product.slug]
        product.slug = slug
        self._by_slug[slug] = product

    def _on_added(self, value: Product) -> None:
        self._by_slug[value.slug] = value

    def _on_removed(self, value: Product) -> None:
        if self._by_slug.get(value.slug) is value:
            del self._by_slug[value.slug]


class CheckoutTable(Table[uuid.UUID, "Checkout"]):
    def __init__(self) -> None:
        super().__init__()
        self._by_token: dict[uuid.UUID, Checkout] = {}

    def find_by_token(self, token: uuid.UUID) -> Checkout | None:
        return self._by_token.get(token)

    def _on_added(self, value: Checkout) -> None:
        self._by_token[value.token] = value

    def _on_removed(self, value: Checkout) -> None:
        self._by_token.pop(value.token, None)


class OrderTable(Table[uuid.UUID, "Order"]):
    def __init__(self) -> None:
        super().__init__()
        self._by_checkout_token: dict[uuid.UUID, Order] = {}

    def find_by_checkout_token(self, token: uuid.UUID) -> Order | None:
        return self._by_checkout_token.get(token)

    def _on_added(self, value: Order) -> None:
        self._by_checkout_token[value.checkout_token] = value

    def _on_removed(self, value: Order) -> None:
        if self._by_checkout_token.get(value.checkout_token) is value:
            del self._by_checkout_token[value.checkout_token]


class UserTable(Table[uuid.UUID, "User"]):
    def __init__(self) -> None:
        super().__init__()
        self._by_email: dict[str, User] = {}

    def find_by_normalized_email(self, normalized_email: str) -> User | None:
        return self._by_email.get(normalized_email)

    def set_email(self, user: User, email: str) -> None:
        if self._by_email.get(user.normalized_email) is user:
            del self._by_email[user.normalized_email]
        user.email = email
        self._by_email[user.normalized_email] = user

    def _on_added(self, value: User) -> None:
        self._by_email[value.normalized_email] = value

    def _on_removed(self, value: User) -> None:
        if self._by_email.get(value.normalized_email) is value:
            del self._by_email[value.normalized_email]


class VoucherTable(Table[uuid.UUID, "Voucher"]):
    def __init__(self) -> None:
        super().__init__()
        self._by_code: dict[str, tuple[Voucher, VoucherCode]] = {}

    def find_by_code(self, normalized_code: str) -> tuple[Voucher | None, VoucherCode | None]:
        hit = self._by_code.get(normalized_code)
        return hit if hit is not None else (None, None)

    def add_codes(self, voucher: Voucher, codes: list[VoucherCode]) -> None:
        for code in codes:
            voucher.codes.append(code)
            self._by_code[code.code] = (voucher, code)

    def _on_added(self, value: Voucher) -> None:
        for code in value.codes:
            self._by_code[code.code] = (value, code)

    def _on_removed(self, value: Voucher) -> None:
        for code in value.codes:
            self._by_code.pop(code.code, None)


class GiftCardTable(Table[uuid.UUID, "GiftCard"]):
    def __init__(self) -> None:
        super().__init__()
        self._by_code: dict[str, GiftCard] = {}

    def find_by_code(self, normalized_code: str) -> GiftCard | None:
        return self._by_code.get(normalized_code)

    def _on_added(self, value: GiftCard) -> None:
        self._by_code[value.code] = value

    def _on_removed(self, value: GiftCard) -> None:
        if self._by_code.get(value.code) is value:
            del self._by_code[value.code]


class ReservationSet:
    def __init__(self) -> None:
        self._all: dict[uuid.UUID, Reservation] = {}
        self._by_stock: MultiIndex[tuple[uuid.UUID | None, uuid.UUID], Reservation] = MultiIndex()
        self._by_checkout: MultiIndex[uuid.UUID, Reservation] = MultiIndex()

    def __len__(self) -> int:
        return len(self._all)

    def __iter__(self) -> Iterator[Reservation]:
        return iter(list(self._all.values()))

    def for_stock(self, warehouse_id: uuid.UUID, variant_id: uuid.UUID) -> list[Reservation]:
        return self._by_stock.get((warehouse_id, variant_id))

    def for_preorder(self, variant_id: uuid.UUID) -> list[Reservation]:
        return self._by_stock.get((None, variant_id))

    def add(self, reservation: Reservation) -> None:
        if reservation.id in self._all:
            raise KeyError(reservation.id)
        self._all[reservation.id] = reservation
        self._by_stock.add((reservation.warehouse_id, reservation.variant_id), reservation)
        self._by_checkout.add(reservation.checkout_id, reservation)

    def remove_for_checkout(self, checkout_id: uuid.UUID) -> int:
        removed = self._by_checkout.take(checkout_id)
        if removed is None:
            return 0
        for r in removed:
            self._all.pop(r.id, None)
            self._by_stock.remove((r.warehouse_id, r.variant_id), r)
        return len(removed)

    def remove_all(self, match: Callable[[Reservation], bool]) -> int:
        removed = [r for r in self._all.values() if match(r)]
        for r in removed:
            del self._all[r.id]
            self._by_stock.remove((r.warehouse_id, r.variant_id), r)
            self._by_checkout.remove(r.checkout_id, r)
        return len(removed)


class PreorderAllocationSet:
    def __init__(self) -> None:
        self._all: list[PreorderAllocation] = []
        self._by_variant: MultiIndex[uuid.UUID, PreorderAllocation] = MultiIndex()

    def __len__(self) -> int:
        return len(self._all)

    def __iter__(self) -> Iterator[PreorderAllocation]:
        return iter(list(self._all))

    def for_variant(self, variant_id: uuid.UUID) -> list[PreorderAllocation]:
        return self._by_variant.get(variant_id)

    def add(self, allocation: PreorderAllocation) -> None:
        self._all.append(allocation)
        self._by_variant.add(allocation.variant_id, allocation)

    def extend(self, allocations: Iterable[PreorderAllocation]) -> None:
        for a in allocations:
            self.add(a)

    def remove(self, allocation: PreorderAllocation) -> bool:
        for i, item in enumerate(self._all):
            if item is allocation:
                del self._all[i]
                self._by_variant.remove(allocation.variant_id, allocation)
                return True
        return False
