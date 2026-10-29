from datetime import datetime, timezone
from uuid import uuid4

import jwt
import pytest

from backend.app.auth import AUDIENCE, ISSUER, TokenCodec, hash_password, verify_password
from backend.app.config import AuthSettings
from backend.app.core.errors import BackendError
from backend.app.schemas.auth import RegisterRequest, TokenResponse


JWT_SECRET = "b02-tests-only-random-key-material-" * 2
PASSWORD = "correct horse battery staple"


def claims_for(user_id=None):
    now = int(datetime.now(timezone.utc).timestamp())
    return {
        "sub": str(user_id or uuid4()), "iat": now, "nbf": now, "exp": now + 900,
        "iss": ISSUER, "aud": AUDIENCE, "token_type": "access",
    }


def test_argon2id_random_salts_and_password_verification():
    first = hash_password(PASSWORD)
    second = hash_password(PASSWORD)
    assert first != second and first.startswith("$argon2id$v=19$m=65536,t=3,p=1$")
    assert PASSWORD not in first
    assert verify_password(PASSWORD, first)
    assert not verify_password("wrong password", first)
    assert not verify_password(PASSWORD, None)
    assert not verify_password(PASSWORD, "legacy-opaque-hash")
    assert not verify_password(PASSWORD, "$argon2id$malformed")


def test_whitespace_and_unicode_passwords_are_not_trimmed_or_truncated():
    password = "  密码包含空格而且不做隐式转换  "
    encoded = hash_password(password)
    assert verify_password(password, encoded)
    assert not verify_password(password.strip(), encoded)
    assert not verify_password(password + "extra", encoded)


def test_bearer_claims_round_trip_and_sensitive_reprs_are_hidden():
    settings = AuthSettings(JWT_SECRET)
    codec = TokenCodec(settings)
    user_id = uuid4()
    token = codec.issue(user_id)
    assert codec.subject(token.access_token) == user_id
    assert token.expires_in == 900
    assert token.access_token not in repr(token)
    assert JWT_SECRET not in repr(settings)
    assert PASSWORD not in repr(RegisterRequest(account_name="alice", password=PASSWORD))
    assert token.access_token not in repr(TokenResponse(access_token=token.access_token, expires_in=900))
    claims = jwt.decode(token.access_token, JWT_SECRET, algorithms=["HS256"], audience=AUDIENCE, issuer=ISSUER)
    assert set(claims) == {"sub", "iat", "nbf", "exp", "iss", "aud", "token_type"}
    assert claims["exp"] - claims["iat"] == 900


@pytest.mark.parametrize("field", ["sub", "iat", "nbf", "exp", "iss", "aud", "token_type"])
def test_jwt_requires_every_access_claim(field):
    claims = claims_for()
    claims.pop(field)
    token = jwt.encode(claims, JWT_SECRET, algorithm="HS256")
    with pytest.raises(BackendError) as failure:
        TokenCodec(AuthSettings(JWT_SECRET)).subject(token)
    assert failure.value.code == "unauthenticated"
    assert token not in str(failure.value)


@pytest.mark.parametrize("change", [
    {"sub": "not-a-uuid"}, {"sub": 42}, {"iss": "other-app"}, {"aud": "other-api"},
    {"aud": [AUDIENCE]}, {"token_type": "refresh"}, {"exp": 1}, {"exp": "9999999999"},
    {"iat": True}, {"iat": 9999999999}, {"nbf": 9999999999}, {"exp": 9999999999},
])
def test_invalid_claims_are_rejected(change):
    claims = {**claims_for(), **change}
    token = jwt.encode(claims, JWT_SECRET, algorithm="HS256")
    with pytest.raises(BackendError) as failure:
        TokenCodec(AuthSettings(JWT_SECRET)).subject(token)
    assert failure.value.code == "unauthenticated"


@pytest.mark.parametrize("algorithm,key", [("none", ""), ("HS384", JWT_SECRET), ("HS256", "different-test-key-material-" * 2)])
def test_unsigned_wrong_algorithm_and_wrong_signature_tokens_are_rejected(algorithm, key):
    token = jwt.encode(claims_for(), key, algorithm=algorithm)
    with pytest.raises(BackendError) as failure:
        TokenCodec(AuthSettings(JWT_SECRET)).subject(token)
    assert failure.value.code == "unauthenticated"


@pytest.mark.parametrize("settings", [
    {"jwt_secret": ""}, {"jwt_secret": "short"},
    {"jwt_secret": "replace-with-a-long-but-public-placeholder-key"},
    {"jwt_secret": " " + JWT_SECRET}, {"jwt_algorithm": "none"}, {"jwt_algorithm": "HS384"},
    {"access_token_ttl_seconds": 0}, {"access_token_ttl_seconds": -1},
    {"access_token_ttl_seconds": 3601}, {"access_token_ttl_seconds": True},
])
def test_invalid_auth_settings_fail_without_exposing_the_secret(settings):
    with pytest.raises(ValueError) as failure:
        AuthSettings(**{"jwt_secret": JWT_SECRET, **settings})
    assert JWT_SECRET not in str(failure.value)


def test_environment_loads_auth_settings_and_hides_invalid_values(monkeypatch):
    monkeypatch.setenv("PYCODE_JWT_SECRET", JWT_SECRET)
    monkeypatch.setenv("PYCODE_JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("PYCODE_ACCESS_TOKEN_TTL_SECONDS", "300")
    settings = AuthSettings.from_environment()
    assert settings.jwt_secret == JWT_SECRET and settings.access_token_ttl_seconds == 300
    monkeypatch.setenv("PYCODE_ACCESS_TOKEN_TTL_SECONDS", "private-invalid-config")
    with pytest.raises(ValueError) as failure:
        AuthSettings.from_environment()
    assert "private-invalid-config" not in str(failure.value)
