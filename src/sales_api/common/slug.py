from __future__ import annotations

import unicodedata
from collections.abc import Callable

from .errors import DomainException, ErrorCodes

_TRANSLITERATION = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
    "ä": "ae",
    "ö": "oe",
    "ü": "ue",
    "ß": "ss",
}


def _is_ascii_letter_or_digit(c: str) -> bool:
    return c.isascii() and c.isalnum()


def slugify(text: str) -> str:
    out: list[str] = []
    for raw in text.strip().lower():
        mapped = _TRANSLITERATION.get(raw)
        if mapped is not None:
            out.append(mapped)
            continue

        # strip diacritics
        for c in unicodedata.normalize("NFD", raw):
            if unicodedata.category(c) == "Mn":
                continue
            if _is_ascii_letter_or_digit(c):
                out.append(c)
            elif out and out[-1] != "-":
                out.append("-")

    slug = "".join(out).strip("-")
    return slug or "item"


def unique_slug(requested: str | None, name: str, is_taken: Callable[[str], bool]) -> str:
    if requested is not None and requested.strip():
        normalized = slugify(requested)
        if is_taken(normalized):
            raise DomainException(ErrorCodes.DUPLICATE, "slug", f"Slug '{normalized}' is already in use.")
        return normalized

    base = slugify(name)
    candidate = base
    i = 2
    while is_taken(candidate):
        candidate = f"{base}-{i}"
        i += 1
    return candidate
