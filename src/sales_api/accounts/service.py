from __future__ import annotations

import uuid
from dataclasses import dataclass
from urllib.parse import quote

from ..apps.handlers import parse_absolute_url
from ..common.address import Address
from ..common.errors import DomainException, ErrorCodes, ensure, must_exist
from ..events import payloads
from ..events.domain_event import EventTypes
from ..notifications import NotificationType, Notifier
from ..permissions import resolver
from ..permissions.model import Principal
from ..persistence.store import Store
from ..shop_context import ShopContext, require_user
from .model import CustomerEvent, ExternalIdentity, User, UserAddress
from .security import BruteForceGuard, PasswordHasher, PasswordPolicy, TokenClaims, TokenService, TokenTypes


@dataclass(frozen=True)
class TokenPair:
    access_token: str
    refresh_token: str


@dataclass(frozen=True)
class ExternalIdentityClaims:
    provider: str
    subject: str
    email: str
    email_verified: bool
    first_name: str | None = None
    last_name: str | None = None


def _escape(value: str) -> str:
    return quote(value, safe="")


def _link(redirect_url: str, email: str, token: str) -> str:
    return f"{redirect_url}?email={_escape(email)}&token={_escape(token)}"


def _normalize_email_input(email: str) -> str:
    trimmed = email.strip()
    at = trimmed.find("@")
    ensure(
        trimmed.count("@") == 1 and at > 0 and at < len(trimmed) - 1,
        ErrorCodes.INVALID,
        "email",
        "Enter a valid email address.",
    )
    return trimmed


class AccountService:
    def __init__(self, ctx: ShopContext, notifier: Notifier, password_policy: PasswordPolicy | None = None):
        self._ctx = ctx
        self._notifier = notifier
        self._policy = password_policy or PasswordPolicy()
        self._brute_force = BruteForceGuard()
        self.tokens = TokenService(ctx.options.token_signing_secret)

    def register(
        self, email: str, password: str, redirect_url: str | None = None, language_code: str | None = None
    ) -> User:
        ctx = self._ctx
        email = _normalize_email_input(email)
        self.validate_password(password, email)
        self.validate_redirect(redirect_url)

        def create(store: Store) -> tuple[User, bool]:
            ensure(
                store.find_user_by_email(email) is None,
                ErrorCodes.DUPLICATE,
                "email",
                "A user with this email already exists.",
            )
            require_confirmation = store.settings.enable_account_confirmation_by_email
            created = User(
                email=email,
                password_hash=PasswordHasher.hash(password),
                is_active=not require_confirmation,
                language_code=language_code,
                created_at=ctx.now,
            )
            store.users.add(created.id, created)
            created.events.append(CustomerEvent(ctx.now, "ACCOUNT_CREATED", created.id))
            ctx.raise_event(EventTypes.CUSTOMER_CREATED, payloads.user(created))
            if require_confirmation:
                ctx.raise_event(EventTypes.ACCOUNT_CONFIRMATION_REQUESTED, payloads.user(created))
            return created, require_confirmation

        user, confirmation_required = ctx.db.write(create)
        if confirmation_required:
            self._send_confirmation(user, redirect_url)
        return user

    def request_email_confirmation(self, principal: Principal, redirect_url: str | None = None) -> None:
        user_id = require_user(principal)
        self.validate_redirect(redirect_url)
        user = self._ctx.db.read(lambda store: store.users[user_id])
        ensure(not user.is_confirmed, ErrorCodes.INVALID, "email", "Email is already confirmed.")
        self._send_confirmation(user, redirect_url)

    def confirm_account(self, email: str, token: str) -> None:
        ctx = self._ctx
        claims = self.tokens.validate(token, TokenTypes.ACCOUNT_CONFIRMATION, ctx.now)

        def confirm(store: Store) -> None:
            user = must_exist(store.find_user_by_email(email), "email", email)
            ensure(
                claims.user_id == user.id and claims.extra.get("email") == user.normalized_email,
                ErrorCodes.BAD_TOKEN,
                "token",
                "Invalid token.",
            )
            user.is_active = True
            user.is_confirmed = True
            user.events.append(CustomerEvent(ctx.now, "ACCOUNT_CONFIRMED", user.id))
            link_guest_orders(store, user)
            ctx.raise_event(EventTypes.ACCOUNT_CONFIRMED, payloads.user(user))

        ctx.db.write(confirm)

    def login(self, email: str, password: str, ip_address: str) -> TokenPair:
        ctx = self._ctx
        self._brute_force.ensure_allowed(ip_address, email, ctx.now)
        user = ctx.db.read(lambda store: store.find_user_by_email(email))
        # Run the hash anyway, otherwise timing leaks which emails are registered.
        password_ok = PasswordHasher.verify(
            password, user.password_hash if user is not None else PasswordHasher.dummy_hash()
        )
        if user is None or not password_ok:
            self._brute_force.register_failure(ip_address, email, ctx.now)
            raise DomainException(ErrorCodes.BAD_CREDENTIALS, "email", "Please, enter valid credentials.")

        self._brute_force.register_success(ip_address, email)
        settings = ctx.db.read(lambda s: s.settings)
        if (
            not user.is_confirmed
            and settings.enable_account_confirmation_by_email
            and not settings.allow_login_without_confirmation
            and not user.is_staff
        ):
            raise DomainException(ErrorCodes.ACCOUNT_NOT_CONFIRMED, "email", "Account needs to be confirmed via email.")
        ensure(user.is_active, ErrorCodes.INACTIVE_ACCOUNT, "email", "Account is inactive.")

        # slow - do it outside the lock
        new_hash = PasswordHasher.hash(password) if PasswordHasher.needs_rehash(user.password_hash) else None

        def touch(_: Store) -> None:
            user.last_login_at = ctx.now
            if new_hash is not None:
                user.password_hash = new_hash

        ctx.db.write(touch)
        return self.issue_pair(user)

    def refresh_access_token(self, refresh_token: str) -> str:
        claims = self.tokens.validate(refresh_token, TokenTypes.REFRESH, self._ctx.now)
        user = self._ctx.db.read(
            lambda store: _token_user(store, claims, "refreshToken", "Refresh token is no longer valid.")
        )
        return self._issue_access(user)

    def authenticate(self, access_token: str) -> Principal:
        claims = self.tokens.validate(access_token, TokenTypes.ACCESS, self._ctx.now)

        def resolve(store: Store) -> Principal:
            return resolver.for_user(store, _token_user(store, claims, "token", "Token is no longer valid."))

        return self._ctx.db.read(resolve)

    def revoke_all_tokens(self, principal: Principal) -> None:
        user_id = require_user(principal)

        def revoke(store: Store) -> None:
            store.users[user_id].token_version += 1

        self._ctx.db.write(revoke)

    def request_password_reset(self, email: str, redirect_url: str) -> None:
        ctx = self._ctx
        self.validate_redirect(redirect_url)
        user = ctx.db.read(lambda store: store.find_user_by_email(email))
        if user is None or not user.is_active:
            return
        token = self.tokens.issue(
            TokenTypes.PASSWORD_RESET,
            user.id,
            ctx.now + ctx.options.password_reset_ttl,
            {"stamp": user.password_stamp.hex},
        )
        ctx.db.write(lambda _: ctx.raise_event(EventTypes.PASSWORD_RESET_REQUESTED, payloads.user(user)))
        self._notifier.send(
            NotificationType.STAFF_PASSWORD_RESET if user.is_staff else NotificationType.PASSWORD_RESET,
            user.email,
            user.language_code,
            {"link": _link(redirect_url, user.email, token), "token": token},
        )

    def set_password_with_token(self, email: str, token: str, new_password: str) -> None:
        claims = self.tokens.validate(token, TokenTypes.PASSWORD_RESET, self._ctx.now)

        def change(store: Store) -> None:
            user = must_exist(store.find_user_by_email(email), "email", email)
            ensure(
                claims.user_id == user.id and claims.extra.get("stamp") == user.password_stamp.hex,
                ErrorCodes.BAD_TOKEN,
                "token",
                "Invalid or already used token.",
            )
            self.validate_password(new_password, user.email)
            self._set_password(user, new_password)

        self._ctx.db.write(change)

    def change_password(self, principal: Principal, old_password: str, new_password: str) -> TokenPair:
        user_id = require_user(principal)

        def change(store: Store) -> TokenPair:
            user = store.users[user_id]
            ensure(
                PasswordHasher.verify(old_password, user.password_hash),
                ErrorCodes.BAD_CREDENTIALS,
                "oldPassword",
                "Old password isn't valid.",
            )
            self.validate_password(new_password, user.email)
            self._set_password(user, new_password)
            return self.issue_pair(user)

        return self._ctx.db.write(change)

    def _set_password(self, user: User, password: str) -> None:
        user.password_hash = PasswordHasher.hash(password)
        user.password_stamp = uuid.uuid4()
        user.token_version += 1
        user.events.append(CustomerEvent(self._ctx.now, "PASSWORD_CHANGED", user.id))

    def request_email_change(self, principal: Principal, new_email: str, password: str, redirect_url: str) -> None:
        ctx = self._ctx
        user_id = require_user(principal)
        self.validate_redirect(redirect_url)
        new_email = _normalize_email_input(new_email)

        def check(store: Store) -> User:
            u = store.users[user_id]
            ensure(
                PasswordHasher.verify(password, u.password_hash),
                ErrorCodes.BAD_CREDENTIALS,
                "password",
                "Password isn't valid.",
            )
            ensure(
                store.find_user_by_email(new_email) is None,
                ErrorCodes.DUPLICATE,
                "newEmail",
                "Email is already in use.",
            )
            return u

        user = ctx.db.read(check)
        token = self.tokens.issue(
            TokenTypes.EMAIL_CHANGE,
            user.id,
            ctx.now + ctx.options.email_confirmation_ttl,
            {"new_email": new_email, "old_email": user.normalized_email},
        )
        ctx.db.write(lambda _: ctx.raise_event(EventTypes.ACCOUNT_CHANGE_EMAIL_REQUESTED, payloads.user(user)))
        self._notifier.send(
            NotificationType.EMAIL_CHANGE_CONFIRMATION,
            new_email,
            user.language_code,
            {"link": _link(redirect_url, new_email, token), "token": token},
        )

    def confirm_email_change(self, token: str) -> None:
        ctx = self._ctx
        claims = self.tokens.validate(token, TokenTypes.EMAIL_CHANGE, ctx.now)

        def confirm(store: Store) -> None:
            user = must_exist(store.users.get(claims.user_id), "token", claims.user_id)
            ensure(
                claims.extra.get("old_email") == user.normalized_email,
                ErrorCodes.BAD_TOKEN,
                "token",
                "Token no longer valid.",
            )
            new_email = claims.extra["new_email"]
            ensure(
                store.find_user_by_email(new_email) is None, ErrorCodes.DUPLICATE, "email", "Email is already in use."
            )
            store.users.set_email(user, new_email)
            user.is_confirmed = True
            user.events.append(CustomerEvent(ctx.now, "EMAIL_CHANGED", user.id, new_email))
            link_guest_orders(store, user)
            ctx.raise_event(EventTypes.CUSTOMER_UPDATED, payloads.user(user))

        ctx.db.write(confirm)

    def request_account_deletion(self, principal: Principal, redirect_url: str) -> None:
        ctx = self._ctx
        user_id = require_user(principal)
        self.validate_redirect(redirect_url)
        user = ctx.db.read(lambda store: store.users[user_id])
        ensure(not user.is_staff, ErrorCodes.FORBIDDEN, None, "Staff accounts can't be deleted by their owners.")
        token = self.tokens.issue(TokenTypes.ACCOUNT_DELETION, user.id, ctx.now + ctx.options.email_confirmation_ttl)
        ctx.db.write(lambda _: ctx.raise_event(EventTypes.ACCOUNT_DELETE_REQUESTED, payloads.user(user)))
        self._notifier.send(
            NotificationType.ACCOUNT_DELETE,
            user.email,
            user.language_code,
            {"link": _link(redirect_url, user.email, token), "token": token},
        )

    def confirm_account_deletion(self, principal: Principal, token: str) -> None:
        ctx = self._ctx
        user_id = require_user(principal)
        claims = self.tokens.validate(token, TokenTypes.ACCOUNT_DELETION, ctx.now)
        ensure(claims.user_id == user_id, ErrorCodes.BAD_TOKEN, "token", "Invalid token.")

        def delete(store: Store) -> None:
            user = store.users[claims.user_id]
            ensure(
                not user.is_staff,
                ErrorCodes.FORBIDDEN,
                None,
                "Staff accounts can't be deleted by their owners.",
            )
            delete_user_data(store, user)
            ctx.raise_event(EventTypes.CUSTOMER_DELETED, payloads.user(user))

        ctx.db.write(delete)

    def external_login(self, claims: ExternalIdentityClaims) -> TokenPair:
        ctx = self._ctx

        def resolve(store: Store) -> User:
            identity = ExternalIdentity(claims.provider, claims.subject)
            linked = next((u for u in store.users.values() if identity in u.external_identities), None)
            if linked is not None:
                return linked

            existing = store.find_user_by_email(claims.email)
            if existing is not None:
                if not claims.email_verified:
                    raise DomainException(ErrorCodes.EMAIL_TAKEN, "email", "An account with this email already exists.")
                existing.external_identities.append(identity)
                return existing

            created = User(
                email=_normalize_email_input(claims.email),
                first_name=claims.first_name or "",
                last_name=claims.last_name or "",
                is_active=True,
                is_confirmed=claims.email_verified,
                created_at=ctx.now,
            )
            created.external_identities.append(identity)
            store.users.add(created.id, created)
            if claims.email_verified:
                link_guest_orders(store, created)
            ctx.raise_event(EventTypes.CUSTOMER_CREATED, payloads.user(created))
            return created

        user = ctx.db.write(resolve)
        ensure(user.is_active, ErrorCodes.INACTIVE_ACCOUNT, "email", "Account is inactive.")
        return self.issue_pair(user)

    def add_address(
        self,
        principal: Principal,
        address: Address,
        set_default_shipping: bool = False,
        set_default_billing: bool = False,
    ) -> UserAddress:
        user_id = require_user(principal)
        normalized = self._ctx.addresses.normalize(address)

        def add(store: Store) -> UserAddress:
            user = store.users[user_id]
            saved = UserAddress(uuid.uuid4(), normalized)
            user.addresses.append(saved)
            if set_default_shipping or user.default_shipping_address_id is None:
                user.default_shipping_address_id = saved.id
            if set_default_billing or user.default_billing_address_id is None:
                user.default_billing_address_id = saved.id
            return saved

        return self._ctx.db.write(add)

    def delete_address(self, principal: Principal, address_id: uuid.UUID) -> None:
        user_id = require_user(principal)

        def delete(store: Store) -> None:
            user = store.users[user_id]
            before = len(user.addresses)
            user.addresses[:] = [a for a in user.addresses if a.id != address_id]
            ensure(before - len(user.addresses) == 1, ErrorCodes.NOT_FOUND, "id", "Address not found.")
            if user.default_shipping_address_id == address_id:
                user.default_shipping_address_id = None
            if user.default_billing_address_id == address_id:
                user.default_billing_address_id = None

        self._ctx.db.write(delete)

    def set_language(self, principal: Principal, language_code: str) -> None:
        user_id = require_user(principal)

        def update(store: Store) -> None:
            store.users[user_id].language_code = language_code

        self._ctx.db.write(update)

    def issue_pair(self, user: User) -> TokenPair:
        return TokenPair(
            self._issue_access(user),
            self.tokens.issue(
                TokenTypes.REFRESH, user.id, self._ctx.now + self._ctx.options.refresh_token_ttl, _version(user)
            ),
        )

    def _issue_access(self, user: User) -> str:
        return self.tokens.issue(
            TokenTypes.ACCESS, user.id, self._ctx.now + self._ctx.options.access_token_ttl, _version(user)
        )

    def _send_confirmation(self, user: User, redirect_url: str | None) -> None:
        token = self.tokens.issue(
            TokenTypes.ACCOUNT_CONFIRMATION,
            user.id,
            self._ctx.now + self._ctx.options.email_confirmation_ttl,
            {"email": user.normalized_email},
        )
        self._notifier.send(
            NotificationType.ACCOUNT_CONFIRMATION,
            user.email,
            user.language_code,
            {"link": _link(redirect_url or "http://localhost/confirm", user.email, token), "token": token},
        )

    def validate_password(self, password: str, email: str) -> None:
        errors = self._policy.validate(password, email)
        if errors:
            raise DomainException(errors)

    def validate_redirect(self, redirect_url: str | None) -> None:
        if redirect_url is None:
            return
        allowed = self._ctx.db.read(lambda s: {h.casefold() for h in s.settings.allowed_redirect_hosts})
        parts = parse_absolute_url(redirect_url)
        ensure(
            parts is not None and (parts.hostname or "").casefold() in allowed,
            ErrorCodes.INVALID,
            "redirectUrl",
            "Redirect URL host is not allowed.",
        )


def _version(user: User) -> dict[str, str]:
    return {"ver": str(user.token_version)}


def delete_user_data(store: Store, user: User) -> None:
    from ..checkout.completion import has_accepted_payments

    for checkout in [c for c in store.checkouts.values() if c.user_id == user.id]:
        if has_accepted_payments(store, checkout.id):
            checkout.user_id = None
            continue
        store.reservations.remove_for_checkout(checkout.id)
        store.checkouts.remove(checkout.id)

    for card in store.gift_cards.values():
        if card.owner_user_id == user.id:
            card.owner_user_id = None
    for order in store.orders.values():
        if order.user_id == user.id:
            order.user_id = None
    for group in store.permission_groups.values():
        group.user_ids.discard(user.id)
    user.addresses.clear()
    store.users.remove(user.id)


def link_guest_orders(store: Store, user: User) -> None:
    for order in store.orders.values():
        if order.user_id is None and order.email.casefold() == user.email.casefold():
            order.user_id = user.id


def _token_user(store: Store, claims: TokenClaims, field: str, message: str) -> User:
    """Raises if the user is gone, inactive or token_version was bumped."""
    user = store.users.get(claims.user_id)
    if user is None or not user.is_active or claims.extra.get("ver") != str(user.token_version):
        raise DomainException(ErrorCodes.BAD_TOKEN, field, message)
    return user
