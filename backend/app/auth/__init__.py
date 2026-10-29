"""Password hashing and bounded access tokens; persistence remains in the UoW."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
import secrets
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import HashingError, InvalidHashError, VerificationError
from argon2.low_level import Type
import jwt

from backend.app.config import AuthSettings
from backend.app.core.errors import BackendError


_PASSWORD_HASHER = PasswordHasher(
    time_cost=3, memory_cost=65536, parallelism=1, hash_len=32, salt_len=16, type=Type.ID,
)
ISSUER = "pycode-backend"
AUDIENCE = "pycode-api"


def validate_password(value: str, *, minimum: int = 1) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= 128:
        raise ValueError("Password length is invalid.")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ValueError("Password encoding is invalid.") from None
    return value


def hash_password(password: str) -> str:
    validate_password(password, minimum=15)
    try:
        return _PASSWORD_HASHER.hash(password)
    except HashingError:
        raise BackendError("auth_unavailable", "Authentication is unavailable.") from None


@lru_cache(maxsize=1)
def _dummy_hash() -> str:
    return hash_password(secrets.token_urlsafe(32))


def verify_password(password: str, encoded_hash: str | None) -> bool:
    # Missing users still perform a real password verification. Never compare
    # raw passwords or accept the opaque pre-B02 persistence fixture hashes.
    valid_format = isinstance(encoded_hash, str) and encoded_hash.startswith("$argon2id$")
    candidate = encoded_hash if valid_format else _dummy_hash()
    try:
        verified = _PASSWORD_HASHER.verify(candidate, password)
    except (VerificationError, InvalidHashError):
        return False
    return bool(valid_format and verified)


def unauthenticated() -> BackendError:
    return BackendError("unauthenticated", "Authentication failed.")


@dataclass(frozen=True)
class AccessToken:
    access_token: str = field(repr=False)
    expires_in: int


class TokenCodec:
    def __init__(self, settings: AuthSettings) -> None:
        self.settings = settings

    def issue(self, user_id: UUID) -> AccessToken:
        now = int(datetime.now(timezone.utc).timestamp())
        claims = {
            "sub": str(user_id), "iat": now, "nbf": now,
            "exp": now + self.settings.access_token_ttl_seconds,
            "iss": ISSUER, "aud": AUDIENCE, "token_type": "access",
        }
        token = jwt.encode(claims, self.settings.jwt_secret, algorithm=self.settings.jwt_algorithm)
        return AccessToken(token, self.settings.access_token_ttl_seconds)

    def subject(self, token: str) -> UUID:
        if not isinstance(token, str) or len(token) > 4096:
            raise unauthenticated()
        try:
            claims = jwt.decode(
                token, self.settings.jwt_secret, algorithms=[self.settings.jwt_algorithm],
                issuer=ISSUER, audience=AUDIENCE,
                options={
                    "require": ["sub", "iat", "nbf", "exp", "iss", "aud", "token_type"],
                    "verify_signature": True, "verify_exp": True, "verify_iat": True,
                    "verify_nbf": True, "verify_sub": True, "strict_aud": True,
                },
            )
            if (claims["token_type"] != "access"
                    or any(type(claims[key]) is not int for key in ("iat", "nbf", "exp"))
                    or claims["nbf"] != claims["iat"]
                    or not 0 < claims["exp"] - claims["iat"] <= self.settings.access_token_ttl_seconds):
                raise ValueError("Invalid access claims")
            return UUID(claims["sub"])
        except (jwt.InvalidTokenError, ValueError, TypeError, OverflowError):
            raise unauthenticated() from None
