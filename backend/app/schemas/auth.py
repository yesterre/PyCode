from datetime import datetime, timezone
from typing import ClassVar, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from backend.app.auth import validate_password
from backend.app.core.errors import BackendError
from backend.app.core.models import UserStatus, normalize_account_name
from backend.app.schemas.projects import RequestModel


class LoginRequest(RequestModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    minimum_password_length: ClassVar[int] = 1
    account_name: str = Field(strict=True, max_length=256)
    password: SecretStr

    @field_validator("account_name")
    @classmethod
    def normalize_account(cls, value: str) -> str:
        try:
            return normalize_account_name(value)
        except BackendError:
            raise ValueError("Account name is invalid.") from None

    @field_validator("password", mode="before")
    @classmethod
    def validate_secret(cls, value: str) -> str:
        return validate_password(value, minimum=cls.minimum_password_length)


class RegisterRequest(LoginRequest):
    minimum_password_length: ClassVar[int] = 15


class UserResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    account_name: str
    created_at: datetime
    status: UserStatus

    @field_validator("created_at")
    @classmethod
    def utc_created_at(cls, value: datetime) -> datetime:
        return value.astimezone(timezone.utc)


class TokenResponse(BaseModel):
    access_token: str = Field(repr=False)
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
