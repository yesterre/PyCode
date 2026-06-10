import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pycode.models import ClassInfo, FileInfo, ProjectIndex


def save_index(index: ProjectIndex, output_path: Path) -> None:
    """Save a project index as a readable JSON file."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(index), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_index(index_path: Path) -> ProjectIndex:
    """Load a project index JSON file and rebuild dataclass objects."""
    path = Path(index_path)
    if not path.exists():
        raise FileNotFoundError(f"Index file does not exist: {path}")
    if not path.is_file():
        raise IsADirectoryError(f"Index path is not a file: {path}")

    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return _project_index_from_dict(data)


def _project_index_from_dict(data: dict[str, Any]) -> ProjectIndex:
    return ProjectIndex(
        project_path=data["project_path"],
        files=[_file_info_from_dict(file_data) for file_data in data.get("files", [])],
    )


def _file_info_from_dict(data: dict[str, Any]) -> FileInfo:
    return FileInfo(
        path=data["path"],
        imports=list(data.get("imports", [])),
        classes=[
            _class_info_from_dict(class_data)
            for class_data in data.get("classes", [])
        ],
        functions=list(data.get("functions", [])),
    )


def _class_info_from_dict(data: dict[str, Any]) -> ClassInfo:
    return ClassInfo(
        name=data["name"],
        methods=list(data.get("methods", [])),
    )
