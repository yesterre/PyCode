from pathlib import Path

import pytest

from pycode.models import FileInfo
from pycode.parser import parse_python_file


def test_parse_python_file_extracts_basic_structure(tmp_path: Path) -> None:
    file_path = tmp_path / "sample.py"
    file_path.write_text(
        "\n".join(
            [
                "import os",
                "import sys",
                "from pathlib import Path",
                "from services.user_service import UserService, build_default_service",
                "",
                "class AppRunner:",
                "    def __init__(self):",
                "        setup_runner()",
                "",
                "    async def run_async(self):",
                "        self.load()",
                "",
                "def main():",
                "    service = UserService()",
                "    service.get_user(1)",
                "    build_default_service()",
                "",
                "async def async_main():",
                "    await run_job()",
                "",
                "if __name__ == \"__main__\":",
                "    main()",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_python_file(file_path, tmp_path)

    assert isinstance(result, FileInfo)
    assert result.path == "sample.py"
    assert result.imports == [
        "os",
        "sys",
        "pathlib.Path",
        "services.user_service.UserService",
        "services.user_service.build_default_service",
    ]
    assert result.functions == ["main", "async_main"]
    assert len(result.classes) == 1
    assert result.classes[0].name == "AppRunner"
    assert result.classes[0].methods == ["__init__", "run_async"]
    assert result.call_infos[0].caller == "AppRunner.__init__"
    assert result.call_infos[0].calls == ["setup_runner"]
    assert result.call_infos[1].caller == "AppRunner.run_async"
    assert result.call_infos[1].calls == ["self.load"]
    assert result.call_infos[2].caller == "main"
    assert result.call_infos[2].calls == [
        "UserService",
        "service.get_user",
        "build_default_service",
    ]
    assert result.call_infos[3].caller == "async_main"
    assert result.call_infos[3].calls == ["run_job"]
    assert result.has_main_guard is True


def test_parse_python_file_defaults_to_file_name_without_project_path(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "standalone.py"
    file_path.write_text("def main(): pass", encoding="utf-8")

    result = parse_python_file(file_path)

    assert result.path == "standalone.py"
    assert result.functions == ["main"]
    assert result.call_infos == []
    assert result.has_main_guard is False


def test_parse_python_file_extracts_module_and_attribute_calls(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "calls.py"
    file_path.write_text(
        "\n".join(
            [
                "import os",
                "",
                "def load_config():",
                "    value = os.getenv('MODE')",
                "    print(value)",
                "    return str(value)",
            ]
        ),
        encoding="utf-8",
    )

    result = parse_python_file(file_path, tmp_path)

    assert result.call_infos[0].caller == "load_config"
    assert result.call_infos[0].calls == ["os.getenv", "print", "str"]


def test_parse_python_file_supports_utf8_bom(tmp_path: Path) -> None:
    file_path = tmp_path / "bom_file.py"
    file_path.write_text("\ufeffimport os\n", encoding="utf-8")

    result = parse_python_file(file_path, tmp_path)

    assert result.imports == ["os"]


def test_parse_python_file_raises_for_missing_file(tmp_path: Path) -> None:
    missing_file = tmp_path / "missing.py"

    with pytest.raises(FileNotFoundError):
        parse_python_file(missing_file, tmp_path)


def test_parse_python_file_raises_for_directory(tmp_path: Path) -> None:
    with pytest.raises(IsADirectoryError):
        parse_python_file(tmp_path, tmp_path)


def test_parse_python_file_raises_for_non_python_file(tmp_path: Path) -> None:
    file_path = tmp_path / "README.md"
    file_path.write_text("# demo", encoding="utf-8")

    with pytest.raises(ValueError):
        parse_python_file(file_path, tmp_path)
