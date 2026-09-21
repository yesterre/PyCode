from dataclasses import dataclass


@dataclass
class User:
    user_id: int
    name: str
    email: str

    def display_name(self) -> str:
        return self.name.title()


def create_guest_user() -> User:
    return User(user_id=0, name="guest", email="guest@example.com")
