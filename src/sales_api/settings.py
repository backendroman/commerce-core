from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum, auto

from .common.address import Address
from .common.case_insensitive import CaseInsensitiveSet


class WeightUnit(StrEnum):
    G = auto()
    KG = auto()
    LB = auto()
    OZ = auto()


@dataclass(eq=False)
class ShopSettings:
    name: str = "Shop"
    description: str = ""
    domain: str = "localhost"
    company_address: Address | None = None
    default_from_email: str = "noreply@localhost"
    default_from_name: str = "Shop"

    reserve_stock_minutes_anonymous: int | None = None
    reserve_stock_minutes_authenticated: int | None = None

    max_checkout_line_quantity: int = 50

    enable_account_confirmation_by_email: bool = True
    allow_login_without_confirmation: bool = False
    default_weight_unit: WeightUnit = WeightUnit.KG
    allowed_redirect_hosts: CaseInsensitiveSet = field(default_factory=lambda: CaseInsensitiveSet(["localhost"]))

    additional_order_notification_emails: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DeploymentOptions:
    default_language: str = "en"
    checkout_price_cache_ttl: timedelta = timedelta(hours=1)
    empty_checkout_ttl: timedelta = timedelta(hours=6)
    anonymous_checkout_ttl: timedelta = timedelta(days=30)
    user_checkout_ttl: timedelta = timedelta(days=90)
    sync_app_timeout: timedelta = timedelta(seconds=20)
    max_query_complexity: int = 50_000
    max_query_depth: int = 15
    token_signing_secret: str = "development-secret-change-me-please-32b"
    access_token_ttl: timedelta = timedelta(minutes=5)
    refresh_token_ttl: timedelta = timedelta(days=30)
    password_reset_ttl: timedelta = timedelta(hours=1)
    email_confirmation_ttl: timedelta = timedelta(days=3)
    shipping_app_cache_ttl: timedelta = timedelta(minutes=2)
    export_file_ttl: timedelta = timedelta(days=30)
    allowed_internal_hosts: frozenset[str] = frozenset()
