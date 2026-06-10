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
                "        pass",
                "",
                "    async def run_async(self):",
                "        pass",
                "",
                "def main():",
                "    pass",
                "",
                "async def async_main():",
                "    pass",
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


def test_parse_python_file_defaults_to_file_name_without_project_path(
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "standalone.py"
    file_path.write_text("def main(): pass", encoding="utf-8")

    result = parse_python_file(file_path)

    assert result.path == "standalone.py"
    assert result.functions == ["main"]


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
