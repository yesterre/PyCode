from typing import Annotated

from fastapi import APIRouter, Depends, Response

from backend.app.api.dependencies import get_auth_service, get_current_user
from backend.app.core.models import User
from backend.app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserResponse
from backend.app.schemas.projects import ErrorResponse
from backend.app.services.auth import AuthService


router = APIRouter(
    prefix="/api/v1/auth", tags=["auth"],
    responses={code: {"model": ErrorResponse} for code in (401, 409, 503)},
)
Service = Annotated[AuthService, Depends(get_auth_service)]


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.post("/register", status_code=201, response_model=UserResponse)
def register(body: RegisterRequest, service: Service, response: Response):
    _no_store(response)
    return service.register(body.account_name, body.password.get_secret_value())


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, service: Service, response: Response):
    _no_store(response)
    token = service.login(body.account_name, body.password.get_secret_value())
    return TokenResponse(access_token=token.access_token, expires_in=token.expires_in)


@router.get("/me", response_model=UserResponse)
def me(user: Annotated[User, Depends(get_current_user)], response: Response):
    _no_store(response)
    return user
