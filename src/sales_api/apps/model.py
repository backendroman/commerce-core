from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum, auto

from ..common.case_insensitive import CaseInsensitiveDict
from ..permissions.model import Permission


class AppKind(StrEnum):
    LOCAL = auto()
    THIRD_PARTY = auto()


class AppInstallationStatus(StrEnum):
    PENDING = auto()
    INSTALLED = auto()
    FAILED = auto()


@dataclass(frozen=True)
class AppExtension:
    label: str
    mount: str
    target: str
    url: str


@dataclass(frozen=True)
class ManifestWebhook:
    name: str
    target_url: str
    events: tuple[str, ...]
    query: str


@dataclass(frozen=True)
class AppManifest:
    id: str
    name: str
    version: str
    permissions: tuple[Permission, ...]
    token_target_url: str
    webhooks: tuple[ManifestWebhook, ...]
    extensions: tuple[AppExtension, ...]
    support_url: str | None = None
    logo_url: str | None = None


@dataclass(eq=False, kw_only=True)
class AppToken:
    token_hash: str
    last4: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    name: str = ""


@dataclass(eq=False, kw_only=True)
class App:
    name: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    kind: AppKind = AppKind.LOCAL
    manifest_url: str | None = None
    identifier: str | None = None
    version: str | None = None
    is_active: bool = True
    installation_status: AppInstallationStatus = AppInstallationStatus.INSTALLED
    installation_error: str | None = None
    permissions: set[Permission] = field(default_factory=set)
    tokens: list[AppToken] = field(default_factory=list)
    extensions: list[AppExtension] = field(default_factory=list)
    support_url: str | None = None
    logo_url: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)
    private_metadata: dict[str, str] = field(default_factory=dict)


@dataclass(eq=False, kw_only=True)
class WebhookSubscription:
    app_id: uuid.UUID
    target_url: str
    query: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    name: str = ""
    is_active: bool = True
    event_types: set[str] = field(default_factory=set)
    channel_filter: set[uuid.UUID] = field(default_factory=set)
    custom_headers: CaseInsensitiveDict[str] = field(default_factory=CaseInsensitiveDict)


class DeliveryStatus(StrEnum):
    PENDING = auto()
    SUCCESS = auto()
    FAILED = auto()


@dataclass(frozen=True)
class DeliveryAttempt:
    at: datetime
    response_status_code: int | None
    response_body: str | None
    duration: timedelta


@dataclass(eq=False, kw_only=True)
class WebhookDelivery:
    event_id: uuid.UUID
    event_type: str
    subscription_id: uuid.UUID
    app_id: uuid.UUID
    target_url: str
    payload: str
    id: uuid.UUID = field(default_factory=uuid.uuid4)
    issued_at: datetime
    status: DeliveryStatus = DeliveryStatus.PENDING
    next_attempt_at: datetime | None = None
    attempts: list[DeliveryAttempt] = field(default_factory=list)
    custom_headers: Mapping[str, str] = field(default_factory=dict)
