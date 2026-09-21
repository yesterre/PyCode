import os
from pathlib import Path

from controllers.user_controller import UserController, create_user_controller
from services.user_service import UserService, preview_user_name
from utils.formatting import format_user_name


def load_config(config_path: Path) -> dict[str, str]:
    return {
        "config_path": str(config_path),
        "mode": os.getenv("DEMO_MODE", "local"),
    }


async def async_main() -> None:
    service = UserService()
    user = service.get_user(1)
    print(preview_user_name(user))


class AppRunner:
    def __init__(self, controller: UserController) -> None:
        self.controller = controller

    def run(self) -> None:
        user_text = self.controller.show_user(1)
        guest_text = self.controller.show_guest()
        print(f"Hello, {format_user_name(user_text)}")
        print(f"Guest: {guest_text}")


def main() -> None:
    config = load_config(Path("settings.toml"))
    controller = create_user_controller()
    runner = AppRunner(controller)
    print(f"Running in {config['mode']} mode")
    runner.run()


if __name__ == "__main__":
    main()
