from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from ..common.errors import DomainException, ErrorCodes, ensure, must_exist
from ..events import payloads
from ..events.domain_event import EventTypes
from ..permissions.model import Permission, Principal
from ..persistence.store import Store
from ..shop_context import ShopContext, require_permission
from .model import CountryTaxRates, TaxAppFailureBehavior, TaxClass, TaxCountryException, TaxStrategy


@dataclass(frozen=True, kw_only=True)
class TaxConfigurationInput:
    charge_taxes: bool | None = None
    prices_entered_with_tax: bool | None = None
    display_gross_prices: bool | None = None
    strategy: TaxStrategy | None = None
    tax_app_id: uuid.UUID | None = None
    app_failure_behavior: TaxAppFailureBehavior | None = None
    country_exceptions: Mapping[str, TaxCountryException] | None = None


class TaxService:
    def __init__(self, ctx: ShopContext):
        self._ctx = ctx

    def create_tax_class(
        self, principal: Principal, name: str, country_rates: Mapping[str, Decimal] | None = None
    ) -> TaxClass:
        require_permission(principal, Permission.MANAGE_TAXES)

        def create(store: Store) -> TaxClass:
            tax_class = TaxClass(name=name)
            store.tax_classes[tax_class.id] = tax_class
            for country, rate in (country_rates or {}).items():
                _rates_for(store, country).class_rates[tax_class.id] = _valid_rate(rate)
            return tax_class

        return self._ctx.db.write(create)

    def set_country_default_rate(self, principal: Principal, country: str, rate: Decimal | None) -> None:
        require_permission(principal, Permission.MANAGE_TAXES)

        def update(store: Store) -> None:
            _rates_for(store, country).default_rate = None if rate is None else _valid_rate(rate)

        self._ctx.db.write(update)

    def set_class_rate(self, principal: Principal, country: str, tax_class_id: uuid.UUID, rate: Decimal) -> None:
        require_permission(principal, Permission.MANAGE_TAXES)

        def update(store: Store) -> None:
            must_exist(store.tax_classes.get(tax_class_id), "taxClass", tax_class_id)
            _rates_for(store, country).class_rates[tax_class_id] = _valid_rate(rate)

        self._ctx.db.write(update)

    def configure(self, principal: Principal, channel_id: uuid.UUID, data: TaxConfigurationInput) -> None:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_TAXES)

        def configure(store: Store) -> None:
            channel = must_exist(store.channels.get(channel_id), "channel", channel_id)
            app_id = data.tax_app_id or channel.taxes.tax_app_id
            if (data.strategy or channel.taxes.strategy) == TaxStrategy.TAX_APP:
                ensure(
                    app_id is not None and app_id in store.apps,
                    ErrorCodes.INVALID,
                    "taxAppId",
                    "Tax app strategy requires an installed app.",
                )

            c = channel.taxes
            if data.charge_taxes is not None:
                c.charge_taxes = data.charge_taxes
            if data.prices_entered_with_tax is not None:
                c.prices_entered_with_tax = data.prices_entered_with_tax
            if data.display_gross_prices is not None:
                c.display_gross_prices = data.display_gross_prices
            if data.strategy is not None:
                c.strategy = data.strategy
            if data.tax_app_id is not None:
                c.tax_app_id = data.tax_app_id
            if data.app_failure_behavior is not None:
                c.app_failure_behavior = data.app_failure_behavior
            if data.country_exceptions is not None:
                c.country_exceptions.clear()
                for country, exception in data.country_exceptions.items():
                    c.country_exceptions[country.upper()] = exception

            ctx.raise_event(EventTypes.TAX_CONFIGURATION_UPDATED, payloads.channel(channel), channel.id)

        ctx.db.write(configure)


def _rates_for(store: Store, country: str) -> CountryTaxRates:
    code = country.upper()
    rates = store.tax_rates.get(code)
    if rates is None:
        rates = store.tax_rates[code] = CountryTaxRates(country=code)
    return rates


def _valid_rate(rate: Decimal) -> Decimal:
    if 0 <= rate <= 100:
        return rate
    raise DomainException(ErrorCodes.INVALID, "rate", "Rate must be between 0 and 100.")
