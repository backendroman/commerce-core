from __future__ import annotations

import hashlib
import secrets
import threading
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from ..common.errors import DomainException, ErrorCodes, ensure, must_exist
from ..events import selection_projector
from ..permissions import resolver
from ..permissions.model import Permission, Principal
from ..persistence.store import Store
from ..shop_context import ShopContext, require_permission
from .handlers import is_outbound_url_allowed, parse_absolute_url
from .model import App, AppExtension, AppInstallationStatus, AppKind, AppManifest, AppToken, WebhookSubscription

# raised by installers when the app is unreachable
INSTALLER_ERRORS = (OSError, TimeoutError, RuntimeError)


class AppInstaller(Protocol):
    def fetch_manifest(self, manifest_url: str) -> AppManifest: ...

    def deliver_token(self, token_target_url: str, token: str) -> None: ...


@dataclass(frozen=True)
class AppTokenResult:
    app: App
    token: str


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _issue_token(app: App, name: str) -> str:
    raw = secrets.token_urlsafe(30)
    app.tokens.append(AppToken(name=name, token_hash=_hash(raw), last4=raw[-4:]))
    return raw


def _ensure_can_grant(principal: Principal, permissions: Sequence[Permission]) -> None:
    beyond_scope = [p for p in permissions if not principal.has(p)]
    if beyond_scope:
        raise DomainException(
            ErrorCodes.BEYOND_OWN_PERMISSIONS,
            "permissions",
            f"You can't grant permissions you don't have: {', '.join(str(p) for p in beyond_scope)}.",
        )


class AppService:
    def __init__(self, ctx: ShopContext, installer: AppInstaller):
        self._ctx = ctx
        self._installer = installer
        # sha256(token) -> app id
        self._app_by_token_hash: dict[str, uuid.UUID] = {}
        self._cache_lock = threading.Lock()

    def install(self, principal: Principal, manifest_url: str, confirmed_permissions: Sequence[Permission]) -> App:
        ctx = self._ctx
        require_permission(principal, Permission.MANAGE_APPS)
        ensure(
            is_outbound_url_allowed(manifest_url, ctx.options.allowed_internal_hosts, ctx.resolve_host),
            ErrorCodes.INVALID,
            "manifestUrl",
            "Manifest URL is not allowed.",
        )

        try:
            manifest = self._installer.fetch_manifest(manifest_url)
        except INSTALLER_ERRORS as ex:
            fetch_error = str(ex)

            def record_failure(store: Store) -> App:
                failed = App(
                    name=manifest_url,
                    kind=AppKind.THIRD_PARTY,
                    manifest_url=manifest_url,
                    is_active=False,
                    installation_status=AppInstallationStatus.FAILED,
                    installation_error=fetch_error,
                )
                store.apps[failed.id] = failed
                return failed

            return ctx.db.write(record_failure)

        missing = [p for p in manifest.permissions if p not in confirmed_permissions]
        ensure(
            not missing,
            ErrorCodes.INVALID,
            "permissions",
            f"Permissions not confirmed: {', '.join(str(p) for p in missing)}.",
        )
        _ensure_can_grant(principal, manifest.permissions)

        def create(store: Store) -> tuple[App, str]:
            created = App(
                name=manifest.name,
                kind=AppKind.THIRD_PARTY,
                manifest_url=manifest_url,
                identifier=manifest.id,
                version=manifest.version,
                support_url=manifest.support_url,
                logo_url=manifest.logo_url,
            )
            created.permissions.update(manifest.permissions)
            created.extensions.extend(manifest.extensions)
            for webhook in manifest.webhooks:
                selection_projector.parse(webhook.query)
                subscription = WebhookSubscription(
                    app_id=created.id, name=webhook.name, target_url=webhook.target_url, query=webhook.query
                )
                subscription.event_types.update(webhook.events)
                store.subscriptions[subscription.id] = subscription

            store.apps[created.id] = created
            return created, _issue_token(created, "default")

        app, token = ctx.db.write(create)

        try:
            self._installer.deliver_token(manifest.token_target_url, token)
        except INSTALLER_ERRORS as ex:
            delivery_error = str(ex)

            def mark_failed(_: Store) -> None:
                app.installation_status = AppInstallationStatus.FAILED
                app.installation_error = delivery_error
                app.is_active = False

            ctx.db.write(mark_failed)

        return app

    def retry_installation(
        self, principal: Principal, app_id: uuid.UUID, confirmed_permissions: Sequence[Permission]
    ) -> App:
        require_permission(principal, Permission.MANAGE_APPS)
        failed = self._ctx.db.read(lambda store: must_exist(store.apps.get(app_id), "id", app_id))
        if failed.installation_status != AppInstallationStatus.FAILED or failed.manifest_url is None:
            raise DomainException(ErrorCodes.INVALID, "id", "Only failed installations can be retried.")
        self._ctx.db.write(lambda store: store.apps.pop(app_id, None))
        return self.install(principal, failed.manifest_url, confirmed_permissions)

    def create_local_app(self, principal: Principal, name: str, permissions: Sequence[Permission]) -> AppTokenResult:
        require_permission(principal, Permission.MANAGE_APPS)
        _ensure_can_grant(principal, permissions)

        def create(store: Store) -> AppTokenResult:
            app = App(name=name, kind=AppKind.LOCAL)
            app.permissions.update(permissions)
            store.apps[app.id] = app
            return AppTokenResult(app, _issue_token(app, "default"))

        return self._ctx.db.write(create)

    def create_token(self, principal: Principal, app_id: uuid.UUID, name: str) -> str:
        require_permission(principal, Permission.MANAGE_APPS)
        return self._ctx.db.write(lambda store: _issue_token(must_exist(store.apps.get(app_id), "id", app_id), name))

    def subscribe(
        self,
        principal: Principal,
        app_id: uuid.UUID,
        target_url: str,
        event_types: Sequence[str],
        query: str,
        channel_filter: Sequence[uuid.UUID] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> WebhookSubscription:
        require_permission(principal, Permission.MANAGE_APPS)
        selection_projector.parse(query)
        ensure(parse_absolute_url(target_url) is not None, ErrorCodes.INVALID, "targetUrl", "Invalid target URL.")

        def create(store: Store) -> WebhookSubscription:
            must_exist(store.apps.get(app_id), "app", app_id)
            subscription = WebhookSubscription(app_id=app_id, target_url=target_url, query=query)
            subscription.event_types.update(event_types)
            subscription.channel_filter.update(channel_filter or ())
            for k, v in (headers or {}).items():
                subscription.custom_headers[k] = v
            store.subscriptions[subscription.id] = subscription
            return subscription

        return self._ctx.db.write(create)

    def set_active(self, principal: Principal, app_id: uuid.UUID, active: bool) -> None:
        require_permission(principal, Permission.MANAGE_APPS)

        def update(store: Store) -> None:
            must_exist(store.apps.get(app_id), "id", app_id).is_active = active

        self._ctx.db.write(update)

    def delete(self, principal: Principal, app_id: uuid.UUID) -> None:
        require_permission(principal, Permission.MANAGE_APPS)

        def delete(store: Store) -> None:
            must_exist(store.apps.get(app_id), "id", app_id)
            for s in [s for s in store.subscriptions.values() if s.app_id == app_id]:
                del store.subscriptions[s.id]
            del store.apps[app_id]

        self._ctx.db.write(delete)

    def extensions(self, principal: Principal) -> list[tuple[uuid.UUID, AppExtension]]:
        if not principal.is_staff_or_app:
            raise DomainException(ErrorCodes.FORBIDDEN, None, "Staff only.")
        return self._ctx.db.read(
            lambda store: [(a.id, e) for a in store.apps.values() if a.is_active for e in a.extensions]
        )

    def authenticate(self, token: str) -> Principal:
        token_hash = _hash(token)

        def resolve(store: Store) -> Principal:
            app = self._find_by_token_hash(store, token_hash)
            if app is None or not app.is_active:
                raise DomainException(ErrorCodes.BAD_TOKEN, "token", "Invalid or inactive app token.")
            return resolver.for_app(app)

        return self._ctx.db.read(resolve)

    def _find_by_token_hash(self, store: Store, hash_: str) -> App | None:
        with self._cache_lock:
            app_id = self._app_by_token_hash.get(hash_)
        cached = store.apps.get(app_id) if app_id is not None else None
        if cached is not None and any(t.token_hash == hash_ for t in cached.tokens):
            return cached
        app = next((a for a in store.apps.values() if any(t.token_hash == hash_ for t in a.tokens)), None)
        with self._cache_lock:
            if app is None:
                self._app_by_token_hash.pop(hash_, None)
            else:
                self._app_by_token_hash[hash_] = app.id
        return app
