from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from ..common.errors import DomainException, ErrorCodes


@dataclass(frozen=True)
class Field:
    name: str
    children: tuple[Field, ...]


def parse(query: str) -> list[Field]:
    tokens = _tokenize(query)
    fields, position = _parse_set(tokens, 0)
    if position != len(tokens):
        raise _invalid()
    return fields


def project_to_json(query: str, payload: Mapping[str, Any]) -> str:
    return to_json(project(parse(query), payload))


def project(selection: list[Field] | tuple[Field, ...], source: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in selection:
        value = source.get(field.name)
        result[field.name] = _scalar(value) if not field.children else _nested(field.children, value)
    return result


def _nested(children: tuple[Field, ...], value: Any) -> Any:
    if isinstance(value, Mapping):
        return project(children, value)
    if isinstance(value, (list, tuple)):
        return [_nested(children, item) for item in value]
    return None


def _scalar(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return str(value)
    return value


def to_json(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Enum):
        return json.dumps(str(value))
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, uuid.UUID):
        return json.dumps(str(value))
    if isinstance(value, datetime):
        return json.dumps(value.isoformat())
    if isinstance(value, date):
        return json.dumps(value.isoformat())
    if isinstance(value, Mapping):
        return "{" + ",".join(f"{json.dumps(str(k))}:{to_json(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple, set, frozenset)):
        return "[" + ",".join(to_json(v) for v in value) + "]"
    raise TypeError(f"Cannot serialize {type(value).__name__}")


def _parse_set(tokens: list[str], position: int) -> tuple[list[Field], int]:
    if position >= len(tokens) or tokens[position] != "{":
        raise _invalid()
    position += 1
    fields: list[Field] = []
    while position < len(tokens) and tokens[position] != "}":
        name = tokens[position]
        position += 1
        if name in ("{", "}"):
            raise _invalid()
        children: list[Field] = []
        if position < len(tokens) and tokens[position] == "{":
            children, position = _parse_set(tokens, position)
        fields.append(Field(name, tuple(children)))

    if position >= len(tokens):
        raise _invalid()
    return fields, position + 1


def _tokenize(query: str) -> list[str]:
    tokens: list[str] = []
    i = 0
    while i < len(query):
        c = query[i]
        if c.isspace() or c == ",":
            i += 1
            continue
        if c in "{}":
            tokens.append(c)
            i += 1
            continue
        start = i
        while i < len(query) and (query[i].isalnum() or query[i] == "_"):
            i += 1
        if start == i:
            raise _invalid()
        tokens.append(query[start:i])
    return tokens


def _invalid() -> DomainException:
    return DomainException(ErrorCodes.INVALID, "query", "Invalid subscription query.")
