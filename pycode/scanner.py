from pathlib import Path


IGNORED_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules"}


def scan_python_files(project_path: Path) -> list[Path]:
    """Recursively scan a project directory and return Python files."""
    root = Path(project_path)
    if not root.exists():
        raise FileNotFoundError(f"Project path does not exist: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"Project path is not a directory: {root}")

    python_files: list[Path] = []
    for path in root.rglob("*.py"):
        if any(part in IGNORED_DIRS for part in path.relative_to(root).parts):
            continue
        python_files.append(path)

    return sorted(python_files)
