from __future__ import annotations

import ipaddress
import logging
import socket
import threading
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum, auto
from typing import Any, cast
from urllib.parse import SplitResult, urlsplit

from ..channels.model import PaymentAction
from ..payments.model import PaymentMethodDetails

_log = logging.getLogger(__name__)

# Threads of timed-out calls are never killed, so limit how many there can be.
MAX_APP_CALLS_IN_FLIGHT = 64
_in_flight = threading.BoundedSemaphore(MAX_APP_CALLS_IN_FLIGHT)


@dataclass(frozen=True)
class PaymentGateway:
    app_id: uuid.UUID
    id: str
    name: str
    currencies: tuple[str, ...]
    config: Mapping[str, Any]


class PaymentAppOutcome(StrEnum):
    HOLD_PLACED = auto()
    HOLD_DECLINED = auto()
    HOLD_NEEDS_ACTION = auto()
    CHARGE_SETTLED = auto()
    CHARGE_DECLINED = auto()
    CHARGE_NEEDS_ACTION = auto()


@dataclass(frozen=True)
class PaymentAppRequest:
    transaction_id: uuid.UUID
    channel_id: uuid.UUID | None
    currency: str
    amount: Decimal
    action: PaymentAction
    checkout_id: uuid.UUID | None
    data: Mapping[str, Any] | None


@dataclass(frozen=True)
class PaymentAppResponse:
    result: PaymentAppOutcome
    amount: Decimal
    provider_reference: str | None = None
    data: Mapping[str, Any] | None = None
    message: str | None = None
    external_url: str | None = None
    payment_method: PaymentMethodDetails | None = None


@dataclass(frozen=True)
class StoredPaymentMethod:
    id: str
    gateway_id: str
    details: PaymentMethodDetails
    supports_payment: bool


@dataclass(frozen=True)
class TokenizationResponse:
    result: str
    payment_method_id: str | None
    data: Mapping[str, Any] | None
    error: str | None = None


class PaymentAppHandler(ABC):
    @abstractmethod
    def list_gateways(self, checkout_id: uuid.UUID, currency: str) -> list[PaymentGateway]: ...

    @abstractmethod
    def initialize_session(
        self, gateway_id: str, checkout_id: uuid.UUID, data: Mapping[str, Any] | None
    ) -> Mapping[str, Any]: ...

    @abstractmethod
    def start_payment(self, request: PaymentAppRequest) -> PaymentAppResponse: ...

    @abstractmethod
    def continue_payment(self, request: PaymentAppRequest) -> PaymentAppResponse: ...

    @abstractmethod
    def list_stored_payment_methods(self, user_id: uuid.UUID, channel_id: uuid.UUID) -> list[StoredPaymentMethod]: ...

    @abstractmethod
    def delete_stored_payment_method(self, user_id: uuid.UUID, payment_method_id: str) -> bool: ...

    @abstractmethod
    def initialize_tokenization(
        self, user_id: uuid.UUID, channel_id: uuid.UUID, data: Mapping[str, Any] | None
    ) -> TokenizationResponse: ...

    @abstractmethod
    def process_tokenization(
        self, user_id: uuid.UUID, session_id: str, data: Mapping[str, Any] | None
    ) -> TokenizationResponse: ...


@dataclass(frozen=True)
class TaxAppLine:
    line_id: uuid.UUID
    variant_id: uuid.UUID
    quantity: int
    total_price: Decimal
    tax_class_id: uuid.UUID | None
    is_gift_card: bool


@dataclass(frozen=True)
class TaxAppRequest:
    channel_id: uuid.UUID
    currency: str
    prices_entered_with_tax: bool
    country: str
    lines: tuple[TaxAppLine, ...]
    shipping_price: Decimal
    shipping_tax_class_id: uuid.UUID | None
    addresses: Mapping[str, Any]


@dataclass(frozen=True)
class TaxAppLineResult:
    line_id: uuid.UUID
    total_net: Decimal
    total_gross: Decimal
    tax_rate: Decimal


@dataclass(frozen=True)
class TaxAppResponse:
    lines: tuple[TaxAppLineResult, ...] | None
    shipping_net: Decimal
    shipping_gross: Decimal
    shipping_tax_rate: Decimal


class TaxAppHandler(ABC):
    @abstractmethod
    def calculate(self, request: TaxAppRequest) -> TaxAppResponse: ...


@dataclass(frozen=True)
class ShippingAppRequest:
    checkout_id: uuid.UUID
    channel_id: uuid.UUID
    currency: str
    country: str | None
    postal_code: str | None
    subtotal: Decimal
    weight_kg: Decimal


@dataclass(frozen=True)
class ExternalShippingOffer:
    id: str
    name: str
    price: Decimal
    min_days: int | None = None
    max_days: int | None = None


class ShippingAppHandler(ABC):
    @abstractmethod
    def list_shipping_methods(self, request: ShippingAppRequest) -> list[ExternalShippingOffer]: ...


class ShippingFilterAppHandler(ABC):
    @abstractmethod
    def exclude_shipping_methods(self, request: ShippingAppRequest, method_ids: list[str]) -> list[str]: ...


@dataclass(frozen=True)
class AppCallOk[T]:
    value: T


@dataclass(frozen=True)
class AppCallFailed:
    error: str


type AppCallResult[T] = AppCallOk[T] | AppCallFailed


class AppHandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[uuid.UUID, object] = {}

    def register(self, app_id: uuid.UUID, handler: object) -> None:
        self._handlers[app_id] = handler

    def find[H](self, handler_type: type[H], app_id: uuid.UUID) -> H | None:
        handler = self._handlers.get(app_id)
        return handler if isinstance(handler, handler_type) else None

    def all[H](self, handler_type: type[H]) -> Iterator[tuple[uuid.UUID, H]]:
        return ((app_id, h) for app_id, h in list(self._handlers.items()) if isinstance(h, handler_type))


def call_app[T](
    call: Callable[[], T], timeout: timedelta, is_valid: Callable[[T], bool] | None = None
) -> AppCallResult[T]:
    """Run an app callback in a separate thread with a timeout."""
    if not _in_flight.acquire(blocking=False):
        _log.error("App call rejected: %d calls already in flight", MAX_APP_CALLS_IN_FLIGHT)
        return AppCallFailed("Too many app calls in progress.")

    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["value"] = call()
        except Exception as ex:
            _log.warning("App call failed", exc_info=True)
            outcome["error"] = str(ex) or type(ex).__name__
        finally:
            _in_flight.release()

    worker = threading.Thread(target=run, name="app-call", daemon=True)
    try:
        worker.start()
    except BaseException:
        _in_flight.release()
        raise
    worker.join(timeout.total_seconds())
    if worker.is_alive():
        _log.warning("App call timed out after %.1f s", timeout.total_seconds())
        return AppCallFailed("App did not respond in time.")
    if "error" in outcome:
        return AppCallFailed(outcome["error"])
    value = outcome.get("value")
    try:
        valid = value is not None and (is_valid is None or is_valid(value))
    except Exception:
        _log.warning("App response validation raised", exc_info=True)
        valid = False
    if not valid:
        return AppCallFailed("App returned a malformed response.")
    return AppCallOk(cast(T, value))


def parse_absolute_url(url: str) -> SplitResult | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.scheme or not parts.netloc:
        return None
    try:
        _ = parts.port  # ValueError on bad port
    except ValueError:
        return None
    return parts


type HostResolver = Callable[[str], list[str]]


def resolve_host(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return []
    return [str(info[4][0]).split("%", 1)[0] for info in infos]


def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


def is_outbound_url_allowed(
    url: str, allowed_internal_hosts: frozenset[str] | set[str], resolve: HostResolver = resolve_host
) -> bool:
    """SSRF check: http(s) only, and all resolved IPs must be public."""
    parts = parse_absolute_url(url)
    if parts is None or parts.scheme not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower()
    if host in allowed_internal_hosts:
        return True
    try:
        addresses = [str(ipaddress.ip_address(host))]
    except ValueError:
        addresses = resolve(host)
    return bool(addresses) and all(_is_public(a) for a in addresses)
