from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ..common import b64url


@dataclass(frozen=True)
class JsonWebKey:
    kty: str
    kid: str
    use: str
    alg: str
    n: str
    e: str


@dataclass(frozen=True)
class JsonWebKeySet:
    keys: tuple[JsonWebKey, ...]


@dataclass(eq=False)
class _Entry:
    kid: str
    key: rsa.RSAPrivateKey
    published_at: datetime
    retire_after: datetime | None = field(default=None)


def _int_bytes(value: int) -> bytes:
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


class SigningKeyRing:
    def __init__(self, now: datetime):
        self._gate = threading.Lock()
        self._keys: list[_Entry] = []
        self._active_kid = self.publish(now)

    @property
    def active_kid(self) -> str:
        with self._gate:
            return self._active_kid

    def publish(self, now: datetime) -> str:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with self._gate:
            entry = _Entry(uuid.uuid4().hex[:16], key, now)
            self._keys.append(entry)
            return entry.kid

    def activate(self, kid: str, now: datetime, transition: timedelta) -> None:
        with self._gate:
            entry = next((k for k in self._keys if k.kid == kid), None)
            if entry is None:
                raise RuntimeError("Key must be published first.")
            previous = next(k for k in self._keys if k.kid == self._active_kid)
            if previous is not entry:
                previous.retire_after = now + transition
            self._active_kid = entry.kid

    def jwks(self, now: datetime) -> JsonWebKeySet:
        with self._gate:
            self._keys = [k for k in self._keys if k.retire_after is None or k.retire_after > now]
            keys = []
            for k in self._keys:
                numbers = k.key.public_key().public_numbers()
                keys.append(
                    JsonWebKey(
                        "RSA",
                        k.kid,
                        "sig",
                        "RS256",
                        b64url.encode(_int_bytes(numbers.n)),
                        b64url.encode(_int_bytes(numbers.e)),
                    )
                )
            return JsonWebKeySet(tuple(keys))

    def sign_detached(self, payload: str) -> str:
        with self._gate:
            entry = next(k for k in self._keys if k.kid == self._active_kid)
        header = b64url.encode(
            json.dumps({"alg": "RS256", "kid": entry.kid, "b64": False, "crit": ["b64"]}, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        signing_input = (header + ".").encode("ascii") + payload.encode("utf-8")
        signature = entry.key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
        return f"{header}..{b64url.encode(signature)}"

    @staticmethod
    def verify_detached(jws: str, payload: str, jwks: JsonWebKeySet) -> bool:
        parts = jws.split(".")
        if len(parts) != 3 or parts[1] != "":
            return False
        try:
            header = json.loads(b64url.decode(parts[0]))
            signature = b64url.decode(parts[2])
            signing_input = (parts[0] + ".").encode("ascii") + payload.encode("utf-8")
        except ValueError:
            return False
        if not isinstance(header, dict) or header.get("alg") != "RS256":
            return False
        key = next((k for k in jwks.keys if k.kid == header.get("kid")), None)
        if key is None:
            return False

        try:
            public_key = rsa.RSAPublicNumbers(
                int.from_bytes(b64url.decode(key.e), "big"), int.from_bytes(b64url.decode(key.n), "big")
            ).public_key()
            public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
            return True
        except (InvalidSignature, ValueError):
            return False
