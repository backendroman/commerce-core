from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from .shop_context import ShopContext


class NotificationType(StrEnum):
    ACCOUNT_CONFIRMATION = auto()
    PASSWORD_RESET = auto()
    EMAIL_CHANGE_CONFIRMATION = auto()
    ACCOUNT_DELETE = auto()
    ORDER_CONFIRMATION = auto()
    GIFT_CARD_SENT = auto()
    STAFF_INVITE = auto()
    STAFF_PASSWORD_RESET = auto()
    STAFF_NEW_ORDER = auto()
    EXPORT_READY = auto()
    EXPORT_FAILED = auto()


@dataclass(frozen=True)
class EmailMessage:
    type: NotificationType
    to: str
    subject: str
    body: str
    from_email: str
    from_name: str
    language_code: str
    data: Mapping[str, str]


class EmailSender(Protocol):
    def send(self, message: EmailMessage) -> None: ...


class InMemoryEmailSender:
    def __init__(self) -> None:
        self._sent: list[EmailMessage] = []
        self._lock = threading.Lock()

    @property
    def sent(self) -> list[EmailMessage]:
        with self._lock:
            return list(self._sent)

    def send(self, message: EmailMessage) -> None:
        with self._lock:
            self._sent.append(message)

    def to(self, email: str) -> list[EmailMessage]:
        return [m for m in self.sent if m.to.casefold() == email.casefold()]


class NotificationTemplates:
    def __init__(self) -> None:
        self._templates: dict[NotificationType, tuple[str, str]] = {
            NotificationType.ACCOUNT_CONFIRMATION: ("Confirm your account", "Confirm: {link}"),
            NotificationType.PASSWORD_RESET: ("Reset your password", "Reset: {link}"),
            NotificationType.EMAIL_CHANGE_CONFIRMATION: ("Confirm your new email", "Confirm: {link}"),
            NotificationType.ACCOUNT_DELETE: ("Confirm account deletion", "Delete: {link}"),
            NotificationType.ORDER_CONFIRMATION: (
                "Order #{number} received",
                "Thank you for your order #{number}. Total: {total}.",
            ),
            NotificationType.GIFT_CARD_SENT: ("You received a gift card", "Code: {code}, balance: {balance}."),
            NotificationType.STAFF_INVITE: ("You were invited", "Set your password: {link}"),
            NotificationType.STAFF_PASSWORD_RESET: ("Reset your password", "Reset: {link}"),
            NotificationType.STAFF_NEW_ORDER: ("New order #{number}", "Order #{number} was placed in {channel}."),
            NotificationType.EXPORT_READY: ("Your export is ready", "Download: {link}"),
            NotificationType.EXPORT_FAILED: ("Export failed", "Export failed: {error}"),
        }

    def set(self, kind: NotificationType, subject: str, body: str) -> None:
        self._templates[kind] = (subject, body)

    def render(self, kind: NotificationType, data: Mapping[str, str]) -> tuple[str, str]:
        subject, body = self._templates[kind]
        for key, value in data.items():
            subject = subject.replace("{" + key + "}", value)
            body = body.replace("{" + key + "}", value)
        return subject, body


class Notifier:
    def __init__(self, ctx: ShopContext):
        self._ctx = ctx
        self.templates = NotificationTemplates()

    def send(self, kind: NotificationType, to: str, language_code: str | None, data: Mapping[str, str]) -> None:
        settings = self._ctx.db.store.settings
        subject, body = self.templates.render(kind, data)
        self._ctx.mail.send(
            EmailMessage(
                kind,
                to,
                subject,
                body,
                settings.default_from_email,
                settings.default_from_name,
                language_code or self._ctx.options.default_language,
                dict(data),
            )
        )
