from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping

from .errors import DomainException, ErrorCodes


def patch(
    target: MutableMapping[str, str],
    upsert: Mapping[str, str] | None,
    remove: Iterable[str] | None = None,
) -> None:
    for key in remove or ():
        target.pop(key, None)
    for key, value in (upsert or {}).items():
        if not key or not key.strip():
            raise DomainException(ErrorCodes.REQUIRED, "metadata", "Metadata key cannot be empty.")
        target[key] = value


def matches_filter(metadata: Mapping[str, str], key: str, value: str | None) -> bool:
    return key in metadata and (value is None or metadata[key] == value)
