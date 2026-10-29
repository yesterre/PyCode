from backend.app.auth import AccessToken, TokenCodec, hash_password, unauthenticated, verify_password
from backend.app.config import AuthSettings
from backend.app.core.errors import BackendError
from backend.app.core.models import User, UserStatus, normalize_account_name
from backend.app.repositories.interfaces import UnitOfWork


class AuthService:
    def __init__(self, unit_of_work: UnitOfWork, settings: AuthSettings) -> None:
        self.uow = unit_of_work
        self.tokens = TokenCodec(settings)

    def register(self, account_name: str, password: str) -> User:
        normalized = normalize_account_name(account_name)
        encoded_hash = hash_password(password)
        try:
            with self.uow.transaction():
                return self.uow.users.create(normalized, encoded_hash)
        except BackendError as exc:
            if exc.code == "database_conflict":
                raise BackendError("account_conflict", "Account is unavailable.") from None
            raise

    def login(self, account_name: str, password: str) -> AccessToken:
        with self.uow.transaction():
            user = self.uow.users.get_by_account_name(account_name)
        valid_password = verify_password(password, user.password_hash if user is not None else None)
        if not valid_password or user is None or user.status != UserStatus.ACTIVE:
            raise unauthenticated()
        return self.tokens.issue(user.id)

    def current_user(self, token: str) -> User:
        user_id = self.tokens.subject(token)
        try:
            with self.uow.transaction():
                user = self.uow.users.get(user_id)
        except BackendError as exc:
            if exc.code == "user_not_found":
                raise unauthenticated() from None
            raise
        if user.status != UserStatus.ACTIVE:
            raise unauthenticated()
        return user
