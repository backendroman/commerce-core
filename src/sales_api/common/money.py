from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import total_ordering

ZERO = Decimal(0)

# most currencies have 2 decimals
_ZERO_DECIMALS = {"CLP", "ISK", "JPY", "KRW", "VND"}
_THREE_DECIMALS = {"BHD", "JOD", "KWD", "OMR", "TND"}


def round_half_away(amount: Decimal, places: int) -> Decimal:
    exponent = amount.as_tuple().exponent
    if isinstance(exponent, int) and exponent > 0:
        return amount.quantize(Decimal(1))
    if isinstance(exponent, int) and exponent >= -places:
        return amount
    return amount.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def fmt(amount: Decimal) -> str:
    return format(amount, "f")


def decimal_places(currency: str) -> int:
    currency = currency.upper()
    if currency in _ZERO_DECIMALS:
        return 0
    if currency in _THREE_DECIMALS:
        return 3
    return 2


def is_currency_code(code: str | None) -> bool:
    return code is not None and len(code) == 3 and all("A" <= c <= "Z" for c in code)


def round_to_currency(amount: Decimal, currency: str) -> Decimal:
    return round_half_away(amount, decimal_places(currency))


def _same_currency(a: Money, b: Money) -> str:
    if a.currency != b.currency:
        raise ValueError(f"Currency mismatch: {a.currency} vs {b.currency}.")
    return a.currency


@total_ordering
@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    @staticmethod
    def zero(currency: str) -> Money:
        return Money(ZERO, currency)

    def rounded(self) -> Money:
        return Money(round_to_currency(self.amount, self.currency), self.currency)

    def non_negative(self) -> Money:
        return Money(ZERO, self.currency) if self.amount < 0 else self

    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    def __add__(self, other: Money) -> Money:
        return Money(self.amount + other.amount, _same_currency(self, other))

    def __sub__(self, other: Money) -> Money:
        return Money(self.amount - other.amount, _same_currency(self, other))

    def __mul__(self, factor: Decimal | int) -> Money:
        return Money(self.amount * factor, self.currency)

    def __lt__(self, other: Money) -> bool:
        _same_currency(self, other)
        return self.amount < other.amount

    def __str__(self) -> str:
        return f"{fmt(self.amount)} {self.currency}"


@dataclass(frozen=True)
class TaxedMoney:
    net: Money
    gross: Money

    @staticmethod
    def zero(currency: str) -> TaxedMoney:
        return TaxedMoney(Money.zero(currency), Money.zero(currency))

    @staticmethod
    def untaxed(amount: Money) -> TaxedMoney:
        return TaxedMoney(amount, amount)

    @property
    def tax(self) -> Money:
        return self.gross - self.net

    @property
    def currency(self) -> str:
        return self.net.currency

    def __add__(self, other: TaxedMoney) -> TaxedMoney:
        return TaxedMoney(self.net + other.net, self.gross + other.gross)


def money_sum(source: Iterable[Money], currency: str) -> Money:
    return sum(source, Money.zero(currency))


def taxed_sum(source: Iterable[TaxedMoney], currency: str) -> TaxedMoney:
    return sum(source, TaxedMoney.zero(currency))


def dsum(values: Iterable[Decimal]) -> Decimal:
    return sum(values, ZERO)
