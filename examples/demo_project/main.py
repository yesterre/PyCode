import os
from pathlib import Path

from services.user_service import UserService
from utils.formatting import format_user_name


def load_config(config_path: Path) -> dict[str, str]:
    return {
        "config_path": str(config_path),
        "mode": os.getenv("DEMO_MODE", "local"),
    }


async def async_main() -> None:
    service = UserService()
    user = service.get_user(1)
    print(format_user_name(user.name))


class AppRunner:
    def __init__(self, service: UserService) -> None:
        self.service = service

    def run(self) -> None:
        user = self.service.get_user(1)
        print(f"Hello, {format_user_name(user.name)}")


def main() -> None:
    config = load_config(Path("settings.toml"))
    service = UserService()
    runner = AppRunner(service)
    print(f"Running in {config['mode']} mode")
    runner.run()


if __name__ == "__main__":
    main()
