from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from .apps.handlers import AppHandlerRegistry, HostResolver, resolve_host
from .common.address import AddressValidator
from .common.clock import Clock, SystemClock
from .common.errors import DomainException, ErrorCodes
from .events.domain_event import DomainEvent
from .notifications import InMemoryEmailSender
from .permissions.model import Permission, Principal
from .persistence.database import Database, JobQueue
from .settings import DeploymentOptions


class ShopContext:
    def __init__(
        self, options: DeploymentOptions | None = None, clock: Clock | None = None, database: Database | None = None
    ):
        self.options = options or DeploymentOptions()
        self.clock = clock or SystemClock()
        self.db = database or Database()
        self.addresses = AddressValidator()
        self.jobs = JobQueue()
        self.app_handlers = AppHandlerRegistry()
        self.resolve_host: HostResolver = resolve_host
        self.mail = InMemoryEmailSender()

    @property
    def now(self) -> datetime:
        return self.clock.utc_now

    def raise_event(self, kind: str, payload: dict[str, Any], channel_id: uuid.UUID | None = None) -> None:
        self.db.raise_event(DomainEvent(kind, payload, channel_id, occurred_at=self.clock.utc_now))


def require_permission(principal: Principal, permission: Permission) -> None:
    if not principal.has(permission):
        raise DomainException(ErrorCodes.FORBIDDEN, None, f"Requires permission {permission}.")


def require_user(principal: Principal) -> uuid.UUID:
    if principal.user_id is None:
        raise DomainException(ErrorCodes.FORBIDDEN, None, "Requires an authenticated user.")
    return principal.user_id
