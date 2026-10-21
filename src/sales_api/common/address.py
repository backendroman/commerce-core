from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from .errors import ErrorCodes, ErrorCollector


@dataclass(frozen=True, kw_only=True)
class Address:
    first_name: str = ""
    last_name: str = ""
    company_name: str = ""
    street_address_1: str = ""
    street_address_2: str = ""
    city: str = ""
    city_area: str = ""
    postal_code: str = ""
    country: str
    country_area: str = ""
    phone: str = ""
    metadata: Mapping[str, str] = field(default_factory=dict, compare=False)


def _field_path(attribute: str) -> str:
    # street_address_1 -> streetAddress1
    head, *rest = attribute.split("_")
    return head + "".join(part.capitalize() for part in rest)


@dataclass(frozen=True)
class CountryAddressRules:
    country: str
    calling_code: str
    required_fields: frozenset[str]
    postal_code_pattern: str | None
    country_areas: Mapping[str, str]


_BASE_REQUIRED = frozenset({"street_address_1", "city", "country"})
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_PHONE_NOISE = re.compile(r"[\s\-\(\)\.]")
_E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def _req(*extra: str) -> frozenset[str]:
    return _BASE_REQUIRED | set(extra)


class AddressValidator:
    def __init__(self) -> None:
        self._rules: dict[str, CountryAddressRules] = {}
        self.register(
            CountryAddressRules(
                "US",
                "1",
                _req("postal_code", "country_area"),
                r"^\d{5}(-\d{4})?$",
                {
                    "CALIFORNIA": "CA",
                    "NEW YORK": "NY",
                    "TEXAS": "TX",
                    "FLORIDA": "FL",
                    "WASHINGTON": "WA",
                },
            )
        )
        self.register(CountryAddressRules("DE", "49", _req("postal_code"), r"^\d{5}$", {}))
        self.register(CountryAddressRules("FR", "33", _req("postal_code"), r"^\d{2} ?\d{3}$", {}))
        self.register(CountryAddressRules("PL", "48", _req("postal_code"), r"^\d{2}-\d{3}$", {}))
        self.register(CountryAddressRules("RU", "7", _req("postal_code"), r"^\d{6}$", {}))
        self.register(CountryAddressRules("JP", "81", _req("postal_code", "country_area"), r"^\d{3}-?\d{4}$", {}))

    def register(self, rules: CountryAddressRules) -> None:
        self._rules[rules.country.upper()] = rules

    def rules_for(self, country: str) -> CountryAddressRules:
        rules = self._rules.get(country.upper())
        if rules is not None:
            return rules
        return CountryAddressRules(country.upper(), "", _BASE_REQUIRED, None, {})

    def normalize(self, address: Address, field_prefix: str = "address") -> Address:
        errors = ErrorCollector()
        country = address.country.strip().upper()
        if not _COUNTRY.match(country):
            errors.add(ErrorCodes.INVALID, f"{field_prefix}.country", "Country must be an ISO 3166-1 alpha-2 code.")
            errors.raise_if_any()

        rules = self.rules_for(country)
        normalized = dataclasses.replace(
            address,
            country=country,
            first_name=address.first_name.strip(),
            last_name=address.last_name.strip(),
            street_address_1=address.street_address_1.strip(),
            street_address_2=address.street_address_2.strip(),
            city=address.city.strip().upper(),
            postal_code=address.postal_code.strip().upper(),
            country_area=self._normalize_area(address.country_area, rules),
            phone=""
            if not address.phone.strip()
            else self._normalize_phone(address.phone, rules, errors, field_prefix),
        )

        for attribute in sorted(rules.required_fields):
            if not getattr(normalized, attribute).strip():
                label = attribute.replace("_", " ").capitalize()
                errors.add(
                    ErrorCodes.REQUIRED,
                    f"{field_prefix}.{_field_path(attribute)}",
                    f"{label} is required in {country}.",
                )

        pattern = rules.postal_code_pattern
        if pattern is not None and normalized.postal_code and not re.search(pattern, normalized.postal_code):
            errors.add(
                ErrorCodes.INVALID,
                f"{field_prefix}.postalCode",
                f"Postal code '{normalized.postal_code}' is not valid for {country}.",
            )

        if (
            rules.country_areas
            and normalized.country_area
            and normalized.country_area not in rules.country_areas.values()
        ):
            errors.add(
                ErrorCodes.INVALID,
                f"{field_prefix}.countryArea",
                f"Unknown region '{normalized.country_area}' for {country}.",
            )

        errors.raise_if_any()
        return normalized

    @staticmethod
    def _normalize_area(area: str, rules: CountryAddressRules) -> str:
        upper = area.strip().upper()
        return rules.country_areas.get(upper, upper)

    @staticmethod
    def _normalize_phone(phone: str, rules: CountryAddressRules, errors: ErrorCollector, prefix: str) -> str:
        digits = _PHONE_NOISE.sub("", phone.strip())
        if digits.startswith("+"):
            e164 = digits
        elif digits.startswith("00"):
            e164 = "+" + digits[2:]
        elif rules.calling_code:
            e164 = "+" + rules.calling_code + digits.lstrip("0")
        else:
            e164 = "+" + digits

        if not _E164.match(e164):
            errors.add(ErrorCodes.INVALID, f"{prefix}.phone", f"'{phone}' is not a valid phone number.")
        return e164
