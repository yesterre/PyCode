from services.user_service import UserService, build_default_service
from utils.formatting import format_user_name, normalize_email


class UserController:
    def __init__(self, service: UserService) -> None:
        self.service = service

    def show_user(self, user_id: int) -> str:
        user = self.service.get_user(user_id)
        display_name = format_user_name(user.name)
        email = normalize_email(user.email)
        return f"{display_name} <{email}>"

    def show_guest(self) -> str:
        guest = self.service.get_user(0)
        return format_user_name(guest.display_name())


def create_user_controller() -> UserController:
    service = build_default_service()
    return UserController(service)

