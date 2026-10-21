from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum, auto

from ..common.case_insensitive import CaseInsensitiveDict
from ..common.money import ZERO


class TaxStrategy(StrEnum):
    FLAT_RATES = auto()
    TAX_APP = auto()


class TaxAppFailureBehavior(StrEnum):
    ERROR = auto()
    USE_FLAT_RATES = auto()


@dataclass(frozen=True)
class TaxCountryException:
    charge_taxes: bool
    display_gross_prices: bool
    strategy: TaxStrategy
    tax_app_id: uuid.UUID | None = None


@dataclass(eq=False)
class TaxConfiguration:
    charge_taxes: bool = True
    prices_entered_with_tax: bool = True
    display_gross_prices: bool = True
    strategy: TaxStrategy = TaxStrategy.FLAT_RATES
    tax_app_id: uuid.UUID | None = None
    app_failure_behavior: TaxAppFailureBehavior = TaxAppFailureBehavior.ERROR
    country_exceptions: CaseInsensitiveDict[TaxCountryException] = field(default_factory=CaseInsensitiveDict)

    def charge_taxes_for(self, country: str) -> bool:
        e = self.country_exceptions.get(country)
        return e.charge_taxes if e is not None else self.charge_taxes

    def display_gross_for(self, country: str) -> bool:
        e = self.country_exceptions.get(country)
        return e.display_gross_prices if e is not None else self.display_gross_prices

    def strategy_for(self, country: str) -> tuple[TaxStrategy, uuid.UUID | None]:
        e = self.country_exceptions.get(country)
        if e is not None:
            return e.strategy, e.tax_app_id or self.tax_app_id
        return self.strategy, self.tax_app_id


@dataclass(eq=False, kw_only=True)
class TaxClass:
    name: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)


@dataclass(eq=False, kw_only=True)
class CountryTaxRates:
    country: str
    default_rate: Decimal | None = None
    class_rates: dict[uuid.UUID, Decimal] = field(default_factory=dict)

    def rate_for(self, tax_class_id: uuid.UUID | None) -> Decimal:
        if tax_class_id is not None and tax_class_id in self.class_rates:
            return self.class_rates[tax_class_id]
        return self.default_rate if self.default_rate is not None else ZERO
