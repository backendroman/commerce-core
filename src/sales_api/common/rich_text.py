from __future__ import annotations

import copy
import re
from typing import Any

from .errors import DomainException, ErrorCodes

ALLOWED_BLOCK_TYPES = frozenset({"paragraph", "header", "list", "quote", "embed"})

_DANGEROUS_ELEMENTS = re.compile(
    r"<\s*(script|style|iframe|object|embed)\b[^>]*>.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL
)
_DANGEROUS_TAGS = re.compile(r"<\s*/?\s*(script|style|iframe|object|embed)\b[^>]*>", re.IGNORECASE)
_EVENT_HANDLERS = re.compile(r"""\s+on[a-z]+\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)""", re.IGNORECASE)
_DANGEROUS_SCHEME = re.compile(r"^\s*(javascript|vbscript|data)\s*:", re.IGNORECASE)
_DANGEROUS_LINK_ATTRIBUTES = re.compile(
    r"""(href|src)\s*=\s*("|')\s*(javascript|vbscript|data)\s*:[^"']*\2""", re.IGNORECASE
)
_TAGS = re.compile(r"<[^>]+>")


def sanitize(document: dict[str, Any]) -> dict[str, Any]:
    blocks = document.get("blocks") if isinstance(document, dict) else None
    if not isinstance(blocks, list):
        raise DomainException(ErrorCodes.INVALID, "description", "Rich text must contain a 'blocks' array.")

    result = []
    for block in blocks:
        block_type = block.get("type") if isinstance(block, dict) else None
        if not isinstance(block_type, str) or block_type not in ALLOWED_BLOCK_TYPES:
            raise DomainException(ErrorCodes.INVALID, "description", "Unsupported rich text block.")
        result.append(_sanitize_node(block))
    return {"blocks": result}


def sanitize_html(html: str) -> str:
    cleaned = _DANGEROUS_ELEMENTS.sub("", html)
    cleaned = _DANGEROUS_TAGS.sub("", cleaned)
    cleaned = _EVENT_HANDLERS.sub("", cleaned)
    return _DANGEROUS_LINK_ATTRIBUTES.sub(r'\1="#"', cleaned)


def to_plain_text(document: dict[str, Any] | None) -> str:
    blocks = document.get("blocks") if isinstance(document, dict) else None
    if not isinstance(blocks, list):
        return ""
    texts: list[str] = []
    for block in blocks:
        if isinstance(block, dict):
            _collect(block.get("data"), texts)
    return _TAGS.sub(" ", " ".join(texts))


def _collect(node: Any, sink: list[str]) -> None:
    if isinstance(node, str):
        sink.append(node)
    elif isinstance(node, list):
        for item in node:
            _collect(item, sink)
    elif isinstance(node, dict):
        for child in node.values():
            _collect(child, sink)


def _sanitize_node(node: Any) -> Any:
    if isinstance(node, str):
        return "" if _DANGEROUS_SCHEME.search(node) else sanitize_html(node)
    if isinstance(node, list):
        return [_sanitize_node(i) for i in node]
    if isinstance(node, dict):
        return {k: _sanitize_node(v) for k, v in node.items() if not k.lower().startswith("on")}
    return copy.deepcopy(node)
