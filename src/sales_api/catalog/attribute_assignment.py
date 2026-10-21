from __future__ import annotations

import dataclasses
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ..common import rich_text
from ..common.clock import utc
from ..common.errors import ErrorCodes, ErrorCollector
from ..common.slug import slugify
from ..persistence.store import Store
from .model import AssignedAttribute, AttributeInputType, AttributeValue, ProductAttribute, ReferenceTarget


@dataclass(frozen=True, kw_only=True)
class AttributeValueInput:
    attribute_id: uuid.UUID
    values: tuple[str, ...] = ()
    text: str | None = None
    rich_text: dict[str, Any] | None = None
    number: Decimal | None = None
    boolean: bool | None = None
    date: date | None = None
    date_time: datetime | None = None
    file_url: str | None = None
    references: tuple[uuid.UUID, ...] = ()


def resolve(
    store: Store,
    allowed_attribute_ids: Sequence[uuid.UUID],
    inputs: Sequence[AttributeValueInput],
    require_all: bool,
    field: str,
    errors: ErrorCollector,
) -> list[AssignedAttribute]:
    result: list[AssignedAttribute] = []
    for input_ in inputs:
        attribute = store.attributes.get(input_.attribute_id)
        if input_.attribute_id not in allowed_attribute_ids or attribute is None:
            errors.add(
                ErrorCodes.INVALID, field, f"Attribute {input_.attribute_id} is not assigned to this product type."
            )
            continue

        assigned = _resolve_one(store, attribute, input_, field, errors)
        if assigned is not None:
            result.append(assigned)

    if require_all:
        for id_ in allowed_attribute_ids:
            attribute = store.attributes[id_]
            if attribute.value_required and all(a.attribute_id != id_ for a in result):
                errors.add(ErrorCodes.REQUIRED, field, f"Attribute '{attribute.name}' is required.")

    return result


def _resolve_one(
    store: Store, attribute: ProductAttribute, data: AttributeValueInput, field: str, errors: ErrorCollector
) -> AssignedAttribute | None:
    assigned = AssignedAttribute(attribute_id=attribute.id)

    def missing() -> AssignedAttribute | None:
        if attribute.value_required:
            errors.add(ErrorCodes.REQUIRED, field, f"Attribute '{attribute.name}' requires a value.")
        return None

    t = attribute.input_type
    if t in (AttributeInputType.DROPDOWN, AttributeInputType.SWATCH, AttributeInputType.MULTISELECT):
        if t != AttributeInputType.MULTISELECT and len(data.values) > 1:
            errors.add(ErrorCodes.INVALID, field, f"Attribute '{attribute.name}' accepts a single value.")
            return None

        ids: list[uuid.UUID] = []
        for name in data.values:
            value = next(
                (v for v in attribute.values if v.slug == slugify(name) or v.name.casefold() == name.casefold()),
                None,
            )
            if value is None:
                if t == AttributeInputType.SWATCH:
                    errors.add(ErrorCodes.INVALID, field, f"Unknown swatch value '{name}'.")
                    continue
                value = AttributeValue(name=name, slug=slugify(name), sort_order=len(attribute.values))
                attribute.values.append(value)
            ids.append(value.id)

        if not ids and attribute.value_required:
            errors.add(ErrorCodes.REQUIRED, field, f"Attribute '{attribute.name}' requires a value.")
        return dataclasses.replace(assigned, value_ids=tuple(ids))

    if t == AttributeInputType.PLAIN_TEXT:
        return (
            missing() if data.text is None else dataclasses.replace(assigned, text=rich_text.sanitize_html(data.text))
        )
    if t == AttributeInputType.RICH_TEXT:
        return (
            missing()
            if data.rich_text is None
            else dataclasses.replace(assigned, rich_text=rich_text.sanitize(data.rich_text))
        )
    if t == AttributeInputType.NUMERIC:
        return missing() if data.number is None else dataclasses.replace(assigned, number=data.number)
    if t == AttributeInputType.BOOLEAN:
        return missing() if data.boolean is None else dataclasses.replace(assigned, boolean=data.boolean)
    if t == AttributeInputType.DATE:
        return missing() if data.date is None else dataclasses.replace(assigned, date=data.date)
    if t == AttributeInputType.DATE_TIME:
        return missing() if data.date_time is None else dataclasses.replace(assigned, date_time=utc(data.date_time))
    if t == AttributeInputType.FILE:
        return missing() if data.file_url is None else dataclasses.replace(assigned, file_url=data.file_url)
    if t == AttributeInputType.REFERENCE:
        for reference in data.references:
            target = _reference_target_of(store, reference)
            if target is None:
                errors.add(ErrorCodes.NOT_FOUND, field, f"Referenced entity {reference} not found.")
            elif attribute.allowed_reference_targets and target not in attribute.allowed_reference_targets:
                errors.add(ErrorCodes.INVALID, field, f"Attribute '{attribute.name}' cannot reference a {target}.")
        return dataclasses.replace(assigned, references=tuple(data.references))
    return missing()


def _reference_target_of(store: Store, id_: uuid.UUID) -> ReferenceTarget | None:
    if id_ in store.products:
        return ReferenceTarget.PRODUCT
    if id_ in store.variants:
        return ReferenceTarget.VARIANT
    if id_ in store.categories:
        return ReferenceTarget.CATEGORY
    if id_ in store.collections:
        return ReferenceTarget.COLLECTION
    return None


def _s(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def combination_key(attributes: Iterable[AssignedAttribute]) -> str:
    return "|".join(
        f"{a.attribute_id}={','.join(str(v) for v in sorted(a.value_ids))}"
        f"{_s(a.text)}{_s(a.number)}{_s(a.boolean)}{_s(a.date)}{_s(a.date_time)}{_s(a.file_url)}"
        f"{','.join(str(r) for r in a.references)}"
        for a in sorted(attributes, key=lambda a: a.attribute_id)
    )
