from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from ..common.case_insensitive import CaseInsensitiveDict
from ..common.errors import must_exist
from ..events import selection_projector
from ..events.domain_event import DomainEvent
from ..permissions.model import Permission, Principal
from ..persistence.store import Store
from ..shop_context import ShopContext, require_permission
from .handlers import is_outbound_url_allowed
from .model import DeliveryAttempt, DeliveryStatus, WebhookDelivery
from .signing import SigningKeyRing

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class WebhookRequest:
    url: str
    headers: Mapping[str, str]
    body: str


@dataclass(frozen=True)
class WebhookResponse:
    status_code: int
    body: str | None = None


class WebhookTransport(Protocol):
    def send(self, request: WebhookRequest) -> WebhookResponse: ...


class InMemoryWebhookTransport:
    def __init__(self) -> None:
        self._sent: list[WebhookRequest] = []
        self._lock = threading.Lock()
        self.responder: Callable[[WebhookRequest], WebhookResponse] = lambda _: WebhookResponse(200)

    @property
    def sent(self) -> list[WebhookRequest]:
        with self._lock:
            return list(self._sent)

    def send(self, request: WebhookRequest) -> WebhookResponse:
        with self._lock:
            self._sent.append(request)
        return self.responder(request)


class WebhookService:
    MAX_ATTEMPTS = 5
    BASE_RETRY_DELAY = timedelta(seconds=10)

    def __init__(self, ctx: ShopContext, keys: SigningKeyRing, transport: WebhookTransport):
        self._ctx = ctx
        self.keys = keys
        self.transport = transport
        ctx.db.committed.append(self._enqueue)

    def _enqueue(self, events: list[DomainEvent]) -> None:
        ctx = self._ctx

        def create(store: Store) -> None:
            for ev in events:
                for subscription in list(store.subscriptions.values()):
                    if not subscription.is_active or ev.type not in subscription.event_types:
                        continue
                    app = store.apps.get(subscription.app_id)
                    if app is None or not app.is_active:
                        continue
                    if subscription.channel_filter and (
                        ev.channel_id is None or ev.channel_id not in subscription.channel_filter
                    ):
                        continue
                    delivery = WebhookDelivery(
                        event_id=ev.id,
                        event_type=ev.type,
                        subscription_id=subscription.id,
                        app_id=app.id,
                        target_url=subscription.target_url,
                        payload=selection_projector.project_to_json(subscription.query, ev.payload),
                        issued_at=ev.occurred_at,
                        next_attempt_at=ctx.now,
                        custom_headers=dict(subscription.custom_headers),
                    )
                    store.deliveries[delivery.id] = delivery

        ctx.db.write(create)
        ctx.jobs.enqueue("webhooks", self.dispatch_due)

    def dispatch_due(self) -> int:
        due = self._ctx.db.read(
            lambda store: [
                d
                for d in store.deliveries.values()
                if d.status == DeliveryStatus.PENDING
                and d.next_attempt_at is not None
                and d.next_attempt_at <= self._ctx.now
            ]
        )
        for delivery in due:
            self._attempt(delivery)
        return len(due)

    def retry(self, principal: Principal, delivery_id: uuid.UUID) -> None:
        require_permission(principal, Permission.MANAGE_APPS)

        def reset(store: Store) -> WebhookDelivery:
            d = must_exist(store.deliveries.get(delivery_id), "id", delivery_id)
            d.status = DeliveryStatus.PENDING
            d.next_attempt_at = self._ctx.now
            return d

        self._attempt(self._ctx.db.write(reset))

    def deliveries(self, principal: Principal, app_id: uuid.UUID | None = None) -> list[WebhookDelivery]:
        require_permission(principal, Permission.MANAGE_APPS)
        return self._ctx.db.read(
            lambda store: sorted(
                (d for d in store.deliveries.values() if app_id is None or d.app_id == app_id),
                key=lambda d: d.issued_at,
            )
        )

    def preview(self, principal: Principal, query: str, sample: DomainEvent) -> str:
        require_permission(principal, Permission.MANAGE_APPS)
        return selection_projector.project_to_json(query, sample.payload)

    def send_test(self, principal: Principal, subscription_id: uuid.UUID, sample: DomainEvent) -> WebhookDelivery:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_APPS)

        def create(store: Store) -> WebhookDelivery:
            s = must_exist(store.subscriptions.get(subscription_id), "id", subscription_id)
            d = WebhookDelivery(
                event_id=sample.id,
                event_type=sample.type,
                subscription_id=s.id,
                app_id=s.app_id,
                target_url=s.target_url,
                payload=selection_projector.project_to_json(s.query, sample.payload),
                issued_at=ctx.now,
                next_attempt_at=ctx.now,
                custom_headers=dict(s.custom_headers),
            )
            store.deliveries[d.id] = d
            return d

        delivery = ctx.db.write(create)
        self._attempt(delivery)
        return delivery

    def _attempt(self, delivery: WebhookDelivery) -> None:
        ctx = self._ctx
        headers: CaseInsensitiveDict[str] = CaseInsensitiveDict(delivery.custom_headers)
        headers["X-Event-Id"] = str(delivery.event_id)
        headers["X-Event-Type"] = delivery.event_type
        headers["X-Issued-At"] = delivery.issued_at.isoformat()
        headers["X-Signature"] = self.keys.sign_detached(delivery.payload)
        headers["Content-Type"] = "application/json"

        started = time.perf_counter()
        if not is_outbound_url_allowed(delivery.target_url, ctx.options.allowed_internal_hosts, ctx.resolve_host):
            response = WebhookResponse(0, "Target address is not allowed.")
        else:
            try:
                response = self.transport.send(WebhookRequest(delivery.target_url, headers, delivery.payload))
            except OSError as ex:
                _log.warning("Webhook delivery %s to %s failed: %s", delivery.id, delivery.target_url, ex)
                response = WebhookResponse(0, str(ex))
        elapsed = timedelta(seconds=time.perf_counter() - started)

        def record(_: Store) -> None:
            delivery.attempts.append(
                DeliveryAttempt(
                    ctx.now, None if response.status_code == 0 else response.status_code, response.body, elapsed
                )
            )
            if 200 <= response.status_code < 300:
                delivery.status = DeliveryStatus.SUCCESS
            elif len(delivery.attempts) >= self.MAX_ATTEMPTS:
                delivery.status = DeliveryStatus.FAILED
            else:
                delivery.next_attempt_at = ctx.now + self.BASE_RETRY_DELAY * (2 ** (len(delivery.attempts) - 1))

        ctx.db.write(record)
