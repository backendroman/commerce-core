from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import threading
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..common import b64url
from ..common.errors import DomainError, DomainException, ErrorCodes
from .model import normalize_email

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class PasswordHasher:
    # OWASP 2023 value for PBKDF2-SHA256
    ITERATIONS = 600_000

    _dummy: tuple[int, str] | None = None

    @staticmethod
    def dummy_hash() -> str:
        """Hash to check against when the user doesn't exist (timing)."""
        cached = PasswordHasher._dummy
        if cached is None or cached[0] != PasswordHasher.ITERATIONS:
            cached = (PasswordHasher.ITERATIONS, PasswordHasher.hash(secrets.token_urlsafe(16)))
            PasswordHasher._dummy = cached
        return cached[1]

    @staticmethod
    def hash(password: str) -> str:
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PasswordHasher.ITERATIONS, 32)
        return f"pbkdf2-sha256:{PasswordHasher.ITERATIONS}:{base64.b64encode(salt).decode()}:{base64.b64encode(digest).decode()}"

    @staticmethod
    def verify(password: str, encoded: str | None) -> bool:
        parts = encoded.split(":") if encoded is not None else None
        if parts is None or len(parts) != 4 or parts[0] != "pbkdf2-sha256":
            return False
        try:
            iterations = int(parts[1])
            salt = base64.b64decode(parts[2], validate=True)
            expected = base64.b64decode(parts[3], validate=True)
        except ValueError:
            return False
        if iterations < 1 or not expected:
            return False
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, len(expected))
        return hmac.compare_digest(expected, actual)

    @staticmethod
    def needs_rehash(encoded: str | None) -> bool:
        parts = encoded.split(":") if encoded is not None else None
        if parts is None or len(parts) != 4 or not parts[1].isdigit():
            return True
        return int(parts[1]) < PasswordHasher.ITERATIONS


@dataclass(frozen=True)
class PasswordPolicy:
    min_length: int = 8
    require_letter_and_digit: bool = True
    disallow_email_in_password: bool = True

    def validate(self, password: str, email: str) -> list[DomainError]:
        errors: list[DomainError] = []
        if len(password) < self.min_length:
            errors.append(
                DomainError(
                    ErrorCodes.WEAK_PASSWORD, "password", f"Password must be at least {self.min_length} characters."
                )
            )
        if self.require_letter_and_digit and not (
            any(c.isalpha() for c in password) and any(c.isdigit() for c in password)
        ):
            errors.append(
                DomainError(ErrorCodes.WEAK_PASSWORD, "password", "Password must contain letters and digits.")
            )
        local = email.split("@")[0]
        if self.disallow_email_in_password and len(local) >= 3 and local.casefold() in password.casefold():
            errors.append(DomainError(ErrorCodes.WEAK_PASSWORD, "password", "Password is too similar to the email."))
        return errors


class TokenTypes:
    ACCESS = "access"
    REFRESH = "refresh"
    PASSWORD_RESET = "password_reset"
    ACCOUNT_CONFIRMATION = "account_confirmation"
    EMAIL_CHANGE = "email_change"
    ACCOUNT_DELETION = "account_deletion"


@dataclass(frozen=True)
class TokenClaims:
    type: str
    user_id: uuid.UUID
    expires_at: datetime
    extra: Mapping[str, str]


def _compact_json(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


class TokenService:
    def __init__(self, secret: str):
        self._key = secret.encode("utf-8")

    def issue(self, kind: str, user_id: uuid.UUID, expires_at: datetime, extra: Mapping[str, str] | None = None) -> str:
        payload: dict[str, object] = {
            "typ": kind,
            "sub": user_id.hex,
            "exp": (expires_at - _EPOCH) // timedelta(milliseconds=1),
            "jti": uuid.uuid4().hex,
        }
        for k, v in (extra or {}).items():
            payload["x_" + k] = v
        header = b64url.encode(_compact_json({"alg": "HS256", "typ": "JWT"}))
        body = b64url.encode(_compact_json(payload))
        return f"{header}.{body}.{self._sign(f'{header}.{body}')}"

    def validate(self, token: str, expected_type: str, now: datetime) -> TokenClaims:
        def invalid() -> DomainException:
            return DomainException(ErrorCodes.BAD_TOKEN, "token", "Invalid token.")

        parts = token.split(".")
        if len(parts) != 3 or not hmac.compare_digest(
            self._sign(f"{parts[0]}.{parts[1]}").encode("utf-8"), parts[2].encode("utf-8")
        ):
            raise invalid()

        try:
            root = json.loads(b64url.decode(parts[1]))
            if not isinstance(root, dict) or root.get("typ") != expected_type:
                raise invalid()
            expires = _EPOCH + timedelta(milliseconds=int(root["exp"]))
            user_id = uuid.UUID(hex=root["sub"])
        except (ValueError, TypeError, KeyError, OverflowError):
            raise invalid() from None
        if expires <= now:
            raise DomainException(ErrorCodes.BAD_TOKEN, "token", "Token has expired.")
        extra = {k[2:]: str(v) for k, v in root.items() if k.startswith("x_")}
        return TokenClaims(expected_type, user_id, expires, extra)

    def _sign(self, data: str) -> str:
        return b64url.encode(hmac.new(self._key, data.encode("utf-8"), hashlib.sha256).digest())


@dataclass
class _FailureState:
    failures: int
    next_allowed: datetime
    last_failure: datetime


class BruteForceGuard:
    MAX_DELAY = timedelta(minutes=15)
    IP_FREE_FAILURES = 10
    FORGET_AFTER = timedelta(hours=24)
    _PRUNE_THRESHOLD = 10_000

    def __init__(self) -> None:
        self._state: dict[str, _FailureState] = {}
        self._lock = threading.Lock()

    def ensure_allowed(self, ip: str, email: str, now: datetime) -> None:
        with self._lock:
            for key in (self._pair_key(ip, email), self._ip_key(ip)):
                state = self._current(key, now)
                if state is not None and state.next_allowed > now:
                    wait = math.ceil((state.next_allowed - now).total_seconds())
                    raise DomainException(ErrorCodes.THROTTLED, None, f"Too many failed attempts. Retry in {wait} s.")

    def register_failure(self, ip: str, email: str, now: datetime) -> None:
        with self._lock:
            if len(self._state) >= self._PRUNE_THRESHOLD:
                self._prune(now)

            pair = self._pair_key(ip, email)
            failures = self._failures(pair, now) + 1
            self._state[pair] = _FailureState(failures, now + self._delay(failures), now)

            ip_key = self._ip_key(ip)
            ip_failures = self._failures(ip_key, now) + 1
            next_allowed = (
                now + self._delay(ip_failures - self.IP_FREE_FAILURES) if ip_failures > self.IP_FREE_FAILURES else now
            )
            self._state[ip_key] = _FailureState(ip_failures, next_allowed, now)

    def register_success(self, ip: str, email: str) -> None:
        # NB: the per-IP counter is not reset here
        with self._lock:
            self._state.pop(self._pair_key(ip, email), None)

    def _current(self, key: str, now: datetime) -> _FailureState | None:
        state = self._state.get(key)
        if state is not None and now - state.last_failure > self.FORGET_AFTER:
            del self._state[key]
            return None
        return state

    def _failures(self, key: str, now: datetime) -> int:
        state = self._current(key, now)
        return 0 if state is None else state.failures

    def _prune(self, now: datetime) -> None:
        stale = [k for k, s in self._state.items() if now - s.last_failure > self.FORGET_AFTER]
        for key in stale:
            del self._state[key]

    @classmethod
    def _delay(cls, failures: int) -> timedelta:
        seconds = 2.0 ** min(failures - 1, 20)
        return timedelta(seconds=min(seconds, cls.MAX_DELAY.total_seconds()))

    @staticmethod
    def _pair_key(ip: str, email: str) -> str:
        return f"pair:{ip}:{normalize_email(email)}"

    @staticmethod
    def _ip_key(ip: str) -> str:
        return f"ip:{ip}"
