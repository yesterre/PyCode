import json
from pathlib import Path

import pytest

from pycode.models import ClassInfo, FileInfo, ProjectIndex
from pycode.storage import load_index, save_index


def test_save_index_creates_parent_directory_and_writes_json(tmp_path: Path) -> None:
    index = ProjectIndex(
        project_path="demo_project",
        files=[
            FileInfo(
                path="main.py",
                imports=["os", "pathlib.Path"],
                classes=[ClassInfo(name="UserService", methods=["get_user"])],
                functions=["main"],
            )
        ],
    )
    output_path = tmp_path / ".pclens" / "index.json"

    save_index(index, output_path)

    assert output_path.exists()
    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert data == {
        "project_path": "demo_project",
        "files": [
            {
                "path": "main.py",
                "imports": ["os", "pathlib.Path"],
                "classes": [
                    {
                        "name": "UserService",
                        "methods": ["get_user"],
                    }
                ],
                "functions": ["main"],
            }
        ],
    }


def test_load_index_rebuilds_project_index_dataclasses(tmp_path: Path) -> None:
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "project_path": "demo_project",
                "files": [
                    {
                        "path": "main.py",
                        "imports": ["os"],
                        "classes": [
                            {
                                "name": "AppRunner",
                                "methods": ["run"],
                            }
                        ],
                        "functions": ["main"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = load_index(index_path)

    assert isinstance(result, ProjectIndex)
    assert result.project_path == "demo_project"
    assert result.files[0].path == "main.py"
    assert result.files[0].imports == ["os"]
    assert result.files[0].classes[0].name == "AppRunner"
    assert result.files[0].classes[0].methods == ["run"]
    assert result.files[0].functions == ["main"]


def test_load_index_supports_utf8_bom(tmp_path: Path) -> None:
    index_path = tmp_path / "index.json"
    index_path.write_text(
        '\ufeff{"project_path": "demo_project", "files": []}',
        encoding="utf-8",
    )

    result = load_index(index_path)

    assert result.project_path == "demo_project"
    assert result.files == []


def test_load_index_raises_for_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_index(tmp_path / "missing.json")


def test_load_index_raises_for_directory(tmp_path: Path) -> None:
    with pytest.raises(IsADirectoryError):
        load_index(tmp_path)
