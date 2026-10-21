from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from ..common.address import Address


@dataclass(frozen=True)
class UserAddress:
    id: uuid.UUID
    address: Address


@dataclass(frozen=True)
class ExternalIdentity:
    provider: str
    subject: str


@dataclass(frozen=True)
class CustomerNote:
    at: datetime
    author_id: uuid.UUID | None
    text: str


@dataclass(frozen=True)
class CustomerEvent:
    at: datetime
    type: str
    actor_id: uuid.UUID | None
    detail: str | None = None


def normalize_email(email: str) -> str:
    return email.strip().casefold()


@dataclass(eq=False, kw_only=True)
class User:
    email: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    password_hash: str | None = None
    first_name: str = ""
    last_name: str = ""
    is_active: bool = True
    is_confirmed: bool = False
    is_staff: bool = False
    is_owner: bool = False
    language_code: str | None = None
    addresses: list[UserAddress] = field(default_factory=list)
    default_shipping_address_id: uuid.UUID | None = None
    default_billing_address_id: uuid.UUID | None = None
    external_identities: list[ExternalIdentity] = field(default_factory=list)
    notes: list[CustomerNote] = field(default_factory=list)
    events: list[CustomerEvent] = field(default_factory=list)

    token_version: int = 0

    password_stamp: uuid.UUID = field(default_factory=uuid.uuid4)

    subscribed_to_order_notifications: bool = False

    created_at: datetime
    last_login_at: datetime | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    private_metadata: dict[str, str] = field(default_factory=dict)

    @property
    def normalized_email(self) -> str:
        return normalize_email(self.email)

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()
