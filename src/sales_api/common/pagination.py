from __future__ import annotations

import base64
import binascii
import functools
import json
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from .errors import DomainException, ErrorCodes

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Page[T]:
    items: list[T]
    end_cursor: str | None
    has_next_page: bool
    total_count: int


class SortDirection(Enum):
    ASC = "asc"
    DESC = "desc"


MAX_PAGE_SIZE = 100


def paginate[T](
    source: Iterable[T],
    sort_key: Callable[[T], Sequence[Any]],
    first: int,
    after: str | None = None,
    direction: SortDirection = SortDirection.ASC,
) -> Page[T]:
    if first < 1 or first > MAX_PAGE_SIZE:
        raise DomainException(ErrorCodes.INVALID, "first", f"Page size must be between 1 and {MAX_PAGE_SIZE}.")

    sign = 1 if direction == SortDirection.ASC else -1
    after_key = None if after is None else _decode_cursor(after)
    keyed: list[tuple[T, list[Any]]] = [(item, list(sort_key(item))) for item in source]

    window = [x for x in keyed if after_key is None or sign * compare_keys(x[1], after_key) > 0]

    def compare(a: tuple[T, list[Any]], b: tuple[T, list[Any]]) -> int:
        return sign * compare_keys(a[1], b[1])

    window.sort(key=functools.cmp_to_key(compare))
    window = window[: first + 1]
    has_next = len(window) > first
    page = window[:first]
    return Page(
        [x[0] for x in page],
        None if not page else _encode_cursor(page[-1][1]),
        has_next,
        len(keyed),
    )


def compare_keys(a: Sequence[Any], b: Sequence[Any]) -> int:
    for x, y in zip(a, b, strict=False):
        if x is None and y is None:
            result = 0
        elif x is None:
            result = -1
        elif y is None:
            result = 1
        else:
            result = (x > y) - (x < y)
        if result != 0:
            return result
    return (len(a) > len(b)) - (len(a) < len(b))


def _encode_value(v: Any) -> str:
    if v is None:
        return "n:"
    if isinstance(v, bool):
        return "b:" + ("1" if v else "0")
    if isinstance(v, str):
        return "s:" + v
    if isinstance(v, Decimal):
        return "d:" + format(v, "f")
    if isinstance(v, int):
        return "i:" + str(v)
    if isinstance(v, uuid.UUID):
        return "g:" + v.hex
    if isinstance(v, datetime):
        return "t:" + str((v - _EPOCH) // timedelta(microseconds=1))
    raise TypeError(f"Unsupported cursor key type {type(v)}")


def _encode_cursor(key: Sequence[Any]) -> str:
    parts = [_encode_value(v) for v in key]
    return base64.b64encode(json.dumps(parts).encode("utf-8")).decode("ascii")


def _decode_value(p: str) -> Any:
    prefix, rest = p[:2], p[2:]
    if prefix == "n:":
        return None
    if prefix == "s:":
        return rest
    if prefix == "d:":
        return Decimal(rest)
    if prefix == "i:":
        return int(rest)
    if prefix == "b:":
        if rest not in ("0", "1"):
            raise ValueError(rest)
        return rest == "1"
    if prefix == "g:":
        return uuid.UUID(hex=rest)
    if prefix == "t:":
        return _EPOCH + timedelta(microseconds=int(rest))
    raise ValueError(p)


def _decode_cursor(cursor: str) -> list[Any]:
    try:
        parts = json.loads(base64.b64decode(cursor, validate=True).decode("utf-8"))
        if not isinstance(parts, list):
            raise ValueError(cursor)
        return [_decode_value(str(p)) for p in parts]
    except (ValueError, TypeError, binascii.Error, InvalidOperation, UnicodeDecodeError):
        raise DomainException(ErrorCodes.INVALID, "after", "Malformed cursor.") from None


class Filter:
    @staticmethod
    def and_(*parts: Filter) -> Filter:
        return AndFilter(tuple(parts))

    @staticmethod
    def or_(*parts: Filter) -> Filter:
        return OrFilter(tuple(parts))

    @staticmethod
    def eq(field: str, value: Any) -> Filter:
        return EqFilter(field, value)

    @staticmethod
    def in_(field: str, *values: Any) -> Filter:
        return InFilter(field, tuple(values))

    @staticmethod
    def range(field: str, gte: Any = None, lte: Any = None) -> Filter:
        return RangeFilter(field, gte, lte)


@dataclass(frozen=True)
class AndFilter(Filter):
    parts: tuple[Filter, ...]


@dataclass(frozen=True)
class OrFilter(Filter):
    parts: tuple[Filter, ...]


@dataclass(frozen=True)
class EqFilter(Filter):
    field: str
    value: Any


@dataclass(frozen=True)
class InFilter(Filter):
    field: str
    values: tuple[Any, ...]


@dataclass(frozen=True)
class RangeFilter(Filter):
    field: str
    gte: Any
    lte: Any


def filter_matches[T](item: T, criteria: Filter | None, fields: Mapping[str, Callable[[T], Any]]) -> bool:
    if criteria is None:
        return True
    if isinstance(criteria, AndFilter):
        return all(filter_matches(item, p, fields) for p in criteria.parts)
    if isinstance(criteria, OrFilter):
        return any(filter_matches(item, p, fields) for p in criteria.parts)
    if isinstance(criteria, EqFilter):
        return any(v == criteria.value for v in _values(item, criteria.field, fields))
    if isinstance(criteria, InFilter):
        return any(v in criteria.values for v in _values(item, criteria.field, fields))
    if isinstance(criteria, RangeFilter):
        return any(
            v is not None
            and (criteria.gte is None or v >= criteria.gte)
            and (criteria.lte is None or v <= criteria.lte)
            for v in _values(item, criteria.field, fields)
        )
    raise NotImplementedError(type(criteria).__name__)


def _values(item: Any, field: str, fields: Mapping[str, Callable[[Any], Any]]) -> list[Any]:
    accessor = fields.get(field)
    if accessor is None:
        raise DomainException(ErrorCodes.INVALID, "filter", f"Unknown filter field '{field}'.")
    value = accessor(item)
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]
