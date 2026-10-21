from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from ..apps.handlers import (
    AppCallFailed,
    AppCallOk,
    AppCallResult,
    TaxAppHandler,
    TaxAppLine,
    TaxAppRequest,
    TaxAppResponse,
    call_app,
)
from ..common.errors import DomainException, ErrorCodes
from ..common.money import ZERO, Money, TaxedMoney, round_to_currency
from .model import TaxAppFailureBehavior, TaxStrategy

if TYPE_CHECKING:
    from ..channels.model import Channel
    from ..persistence.store import Store
    from ..shop_context import ShopContext


@dataclass(frozen=True)
class TaxableLine:
    line_id: uuid.UUID
    variant_id: uuid.UUID
    quantity: int
    total: Decimal
    tax_class_id: uuid.UUID | None
    is_gift_card: bool


@dataclass(frozen=True)
class TaxedLine:
    line_id: uuid.UUID
    total: TaxedMoney
    rate: Decimal


@dataclass(frozen=True)
class TaxCalculation:
    lines: tuple[TaxedLine, ...]
    shipping: TaxedMoney
    shipping_rate: Decimal


@dataclass(frozen=True)
class TaxRequest:
    channel: Channel
    country: str
    lines: tuple[TaxableLine, ...]
    shipping_price: Decimal
    shipping_tax_class_id: uuid.UUID | None
    tax_exempt: bool
    addresses: Mapping[str, Any]


class TaxCalculator:
    def __init__(self, ctx: ShopContext):
        self._ctx = ctx

    def calculate(self, store: Store, request: TaxRequest) -> TaxCalculation:
        config = request.channel.taxes
        charge = config.charge_taxes_for(request.country) and not request.tax_exempt
        if not charge:
            return TaxCalculator.flat(store, request, charge=False)

        strategy, app_id = config.strategy_for(request.country)
        if strategy == TaxStrategy.FLAT_RATES:
            return TaxCalculator.flat(store, request, charge=True)

        result = self._call_app(app_id, request)
        if isinstance(result, AppCallOk):
            return result.value
        if config.app_failure_behavior == TaxAppFailureBehavior.USE_FLAT_RATES:
            return TaxCalculator.flat(store, request, charge=True)
        raise DomainException(ErrorCodes.TAX_CALCULATION_FAILED, None, f"Tax app failed: {result.error}")

    @staticmethod
    def flat(store: Store, request: TaxRequest, charge: bool) -> TaxCalculation:
        currency = request.channel.currency
        entered_gross = request.channel.taxes.prices_entered_with_tax
        rates = store.tax_rates.get(request.country)

        def rate_for(tax_class_id: uuid.UUID | None) -> Decimal:
            if not charge or rates is None:
                return ZERO
            return rates.rate_for(tax_class_id)

        lines = []
        for line in request.lines:
            rate = rate_for(line.tax_class_id)
            lines.append(TaxedLine(line.line_id, TaxCalculator.apply(line.total, rate, entered_gross, currency), rate))

        shipping_rate = rate_for(request.shipping_tax_class_id)
        return TaxCalculation(
            tuple(lines),
            TaxCalculator.apply(request.shipping_price, shipping_rate, entered_gross, currency),
            shipping_rate,
        )

    @staticmethod
    def apply(amount: Decimal, rate_percent: Decimal, entered_gross: bool, currency: str) -> TaxedMoney:
        money = Money(amount, currency)
        if rate_percent == 0:
            return TaxedMoney.untaxed(money)
        if entered_gross:
            net = round_to_currency(amount / (1 + rate_percent / 100), currency)
            return TaxedMoney(Money(net, currency), money)

        tax = round_to_currency(amount * rate_percent / 100, currency)
        return TaxedMoney(money, Money(amount + tax, currency))

    def _call_app(self, app_id: uuid.UUID | None, request: TaxRequest) -> AppCallResult[TaxCalculation]:
        ctx = self._ctx
        if app_id is None:
            return AppCallFailed("No tax app configured.")
        app = ctx.db.read(lambda s: s.apps.get(app_id))
        handler = ctx.app_handlers.find(TaxAppHandler, app_id)
        if app is None or not app.is_active or handler is None:
            return AppCallFailed("Tax app is unavailable.")

        currency = request.channel.currency
        app_request = TaxAppRequest(
            request.channel.id,
            currency,
            request.channel.taxes.prices_entered_with_tax,
            request.country,
            tuple(
                TaxAppLine(
                    line.line_id, line.variant_id, line.quantity, line.total, line.tax_class_id, line.is_gift_card
                )
                for line in request.lines
            ),
            request.shipping_price,
            request.shipping_tax_class_id,
            request.addresses,
        )
        expected_ids = {line.line_id for line in request.lines}

        def valid(r: TaxAppResponse) -> bool:
            return (
                r.lines is not None
                and {x.line_id for x in r.lines} == expected_ids
                and all(x.total_net >= 0 and x.total_gross >= x.total_net for x in r.lines)
                and r.shipping_net >= 0
                and r.shipping_gross >= r.shipping_net
            )

        response = call_app(lambda: handler.calculate(app_request), ctx.options.sync_app_timeout, valid)
        if isinstance(response, AppCallFailed):
            return response

        r = response.value
        lines = tuple(
            TaxedLine(
                x.line_id,
                TaxedMoney(
                    Money(round_to_currency(x.total_net, currency), currency),
                    Money(round_to_currency(x.total_gross, currency), currency),
                ),
                x.tax_rate,
            )
            for x in r.lines or ()  # validated above
        )
        shipping = TaxedMoney(
            Money(round_to_currency(r.shipping_net, currency), currency),
            Money(round_to_currency(r.shipping_gross, currency), currency),
        )
        return AppCallOk(TaxCalculation(lines, shipping, r.shipping_tax_rate))

    @staticmethod
    def catalog_price(
        store: Store, channel: Channel, price: Decimal, tax_class_id: uuid.UUID | None, country: str | None
    ) -> TaxedMoney:
        c = country or channel.default_country
        charge = channel.taxes.charge_taxes_for(c)
        rates = store.tax_rates.get(c)
        rate = rates.rate_for(tax_class_id) if charge and rates is not None else ZERO
        return TaxCalculator.apply(price, rate, channel.taxes.prices_entered_with_tax, channel.currency)
