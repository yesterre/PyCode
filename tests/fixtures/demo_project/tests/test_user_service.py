import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from services.user_service import UserService, preview_user_name


def test_user_service_returns_normalized_user() -> None:
    user = UserService().get_user(1)

    assert user.name == "alice"
    assert user.email == "alice@example.com"


def test_preview_user_name_formats_display_name() -> None:
    user = UserService().get_user(1)

    assert preview_user_name(user) == "Alice"
