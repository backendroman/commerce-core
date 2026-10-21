from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DomainError:
    code: str
    field: str | None
    message: str
    related_ids: tuple[uuid.UUID, ...] = ()


class DomainException(Exception):
    def __init__(
        self, errors: Iterable[DomainError] | DomainError | str, field: str | None = None, message: str | None = None
    ):
        if isinstance(errors, str):
            errors = [DomainError(errors, field, message or "")]
        elif isinstance(errors, DomainError):
            errors = [errors]
        self.errors: list[DomainError] = list(errors)
        super().__init__(
            "; ".join(f"{e.code}{'' if e.field is None else f'[{e.field}]'}: {e.message}" for e in self.errors)
        )

    def has(self, code: str) -> bool:
        return any(e.code == code for e in self.errors)


class ErrorCodes:
    REQUIRED = "required"
    INVALID = "invalid"
    NOT_FOUND = "not_found"
    DUPLICATE = "duplicate"
    FORBIDDEN = "forbidden"
    OUT_OF_SCOPE = "out_of_scope"
    LIMIT_EXCEEDED = "limit_exceeded"

    # Catalog
    CATEGORY_MISSING = "catalog.category_missing"
    VARIANT_DUPLICATE = "catalog.variant_duplicate"
    PREORDER_WAREHOUSE_MISSING = "stock.preorder_warehouse_missing"

    # Checkout & stock
    INSUFFICIENT_STOCK = "stock.insufficient"
    VARIANT_UNAVAILABLE = "checkout.variant_unavailable"
    QUANTITY_LIMIT_EXCEEDED = "checkout.quantity_limit"
    DELIVERY_UNAVAILABLE = "checkout.delivery_unavailable"
    DELIVERY_MISSING = "checkout.delivery_missing"
    SHIPPING_ADDRESS_MISSING = "checkout.shipping_address_missing"
    BILLING_ADDRESS_MISSING = "checkout.billing_address_missing"
    EMAIL_MISSING = "checkout.email_missing"
    CHECKOUT_UNPAID = "checkout.unpaid"
    CHECKOUT_BLOCKED = "checkout.blocked"
    CHANNEL_INACTIVE = "channel.inactive"
    CHECKOUT_EMPTY = "checkout.empty"

    # Discounts
    CODE_REJECTED = "discount.code_rejected"
    GIFT_CARD_REJECTED = "discount.gift_card_rejected"
    GIFT_UNAVAILABLE = "discount.gift_unavailable"

    # Payments & apps
    PAYMENT_FAILED = "payment.failed"
    CURRENCY_MISMATCH = "payment.currency_mismatch"
    APP_FAILED = "app.failed"
    TAX_CALCULATION_FAILED = "tax.failed"

    # Accounts & permissions
    BAD_CREDENTIALS = "auth.bad_credentials"
    THROTTLED = "auth.throttled"
    BAD_TOKEN = "auth.bad_token"
    INACTIVE_ACCOUNT = "auth.inactive"
    ACCOUNT_NOT_CONFIRMED = "auth.unconfirmed"
    EMAIL_TAKEN = "account.email_taken"
    WEAK_PASSWORD = "account.weak_password"
    BEYOND_OWN_PERMISSIONS = "staff.beyond_own_permissions"
    PERMISSION_ORPHANED = "staff.permission_orphaned"
    SELF_CHANGE = "staff.self_change"
    OWNER_ONLY = "staff.owner_only"


class ErrorCollector:
    def __init__(self) -> None:
        self._errors: list[DomainError] = []

    @property
    def has_errors(self) -> bool:
        return len(self._errors) > 0

    @property
    def errors(self) -> list[DomainError]:
        return self._errors

    def add(
        self, code_or_error: str | DomainError, field: str | None = None, message: str | None = None
    ) -> ErrorCollector:
        if isinstance(code_or_error, DomainError):
            self._errors.append(code_or_error)
        else:
            self._errors.append(DomainError(code_or_error, field, message or ""))
        return self

    def raise_if_any(self) -> None:
        if self._errors:
            raise DomainException(list(self._errors))


def must_exist[T](entity: T | None, field: str, key: Any) -> T:
    if entity is None:
        raise DomainException(ErrorCodes.NOT_FOUND, field, f"{field} {key} not found.")
    return entity


def ensure(condition: bool, code: str, field: str | None, message: str) -> None:
    if not condition:
        raise DomainException(code, field, message)
