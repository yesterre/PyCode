import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from pycode.models import (
    CallInfo,
    ClassInfo,
    CodeGraph,
    FileInfo,
    GraphEdge,
    GraphNode,
    ProjectIndex,
)


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


def save_graph(graph: CodeGraph, output_path: Path) -> None:
    """Save a code graph as a readable JSON file."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(asdict(graph), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_graph(graph_path: Path) -> CodeGraph:
    """Load a code graph JSON file and rebuild dataclass objects."""
    path = Path(graph_path)
    if not path.exists():
        raise FileNotFoundError(f"Graph file does not exist: {path}")
    if not path.is_file():
        raise IsADirectoryError(f"Graph path is not a file: {path}")

    data = json.loads(path.read_text(encoding="utf-8-sig"))
    return _code_graph_from_dict(data)


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
        call_infos=[
            _call_info_from_dict(call_data)
            for call_data in data.get("call_infos", [])
        ],
        has_main_guard=bool(data.get("has_main_guard", False)),
    )


def _class_info_from_dict(data: dict[str, Any]) -> ClassInfo:
    return ClassInfo(
        name=data["name"],
        methods=list(data.get("methods", [])),
    )


def _call_info_from_dict(data: dict[str, Any]) -> CallInfo:
    return CallInfo(
        caller=data["caller"],
        calls=list(data.get("calls", [])),
    )


def _code_graph_from_dict(data: dict[str, Any]) -> CodeGraph:
    return CodeGraph(
        project_path=data["project_path"],
        nodes=[_graph_node_from_dict(node_data) for node_data in data.get("nodes", [])],
        edges=[_graph_edge_from_dict(edge_data) for edge_data in data.get("edges", [])],
    )


def _graph_node_from_dict(data: dict[str, Any]) -> GraphNode:
    return GraphNode(
        id=data["id"],
        type=data["type"],
        name=data["name"],
        path=data.get("path"),
    )


def _graph_edge_from_dict(data: dict[str, Any]) -> GraphEdge:
    return GraphEdge(
        source=data["source"],
        target=data["target"],
        type=data["type"],
    )
