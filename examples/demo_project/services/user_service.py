from models.user import User, create_guest_user
from utils.formatting import format_user_name, normalize_email


class UserService:
    def get_user(self, user_id: int) -> User:
        if user_id == 0:
            return create_guest_user()
        return User(
            user_id=user_id,
            name="alice",
            email=normalize_email("ALICE@EXAMPLE.COM"),
        )

    async def get_user_async(self, user_id: int) -> User:
        return self.get_user(user_id)

    def list_users(self) -> list[User]:
        return [
            self.get_user(1),
            create_guest_user(),
        ]


def build_default_service() -> UserService:
    return UserService()


def preview_user_name(user: User) -> str:
    return format_user_name(user.display_name())
