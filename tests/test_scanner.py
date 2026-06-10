from pathlib import Path

import pytest

from pycode.scanner import scan_python_files


def test_scan_python_files_returns_python_files_recursively(tmp_path: Path) -> None:
    (tmp_path / "main.py").write_text("print('hello')", encoding="utf-8")
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "service.py").write_text("class Service: pass", encoding="utf-8")
    (tmp_path / "README.md").write_text("# demo", encoding="utf-8")

    result = scan_python_files(tmp_path)

    relative_paths = [path.relative_to(tmp_path) for path in result]
    assert relative_paths == [
        Path("main.py"),
        Path("package") / "service.py",
    ]
    assert all(isinstance(path, Path) for path in result)


def test_scan_python_files_ignores_configured_directories(tmp_path: Path) -> None:
    (tmp_path / "app.py").write_text("def run(): pass", encoding="utf-8")

    ignored_dirs = [".git", ".venv", "venv", "__pycache__", "node_modules"]
    for dirname in ignored_dirs:
        ignored_dir = tmp_path / dirname
        ignored_dir.mkdir()
        (ignored_dir / "ignored.py").write_text(
            "def should_not_be_scanned(): pass",
            encoding="utf-8",
        )

    result = scan_python_files(tmp_path)

    assert [path.relative_to(tmp_path) for path in result] == [Path("app.py")]


def test_scan_python_files_raises_for_missing_path(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing"

    with pytest.raises(FileNotFoundError):
        scan_python_files(missing_path)


def test_scan_python_files_raises_for_file_path(tmp_path: Path) -> None:
    file_path = tmp_path / "main.py"
    file_path.write_text("print('hello')", encoding="utf-8")

    with pytest.raises(NotADirectoryError):
        scan_python_files(file_path)
