from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pycode.agent.memory import MemoryIndexEntry, MemoryStore
from pycode.agent.task_dag import TaskDAGStore, TaskNode
from pycode.constants import DEFAULT_ARTIFACT_DIR, DEFAULT_GRAPH_FILE, DEFAULT_INDEX_FILE
from pycode.models import CodeGraph, GraphEdge, ProjectIndex
from pycode.storage import load_graph, load_index
from pycode.utils import count_by_type


@dataclass
class ProjectUIData:
    project_path: Path
    index_path: Path
    graph_path: Path
    index: ProjectIndex | None = None
    graph: CodeGraph | None = None
    memories: list[MemoryIndexEntry] = field(default_factory=list)
    tasks: list[TaskNode] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def has_artifacts(self) -> bool:
        return self.index is not None and self.graph is not None


def load_project_ui_data(project_path: str | Path) -> ProjectUIData:
    root = Path(project_path).expanduser()
    index_path = root / DEFAULT_ARTIFACT_DIR / DEFAULT_INDEX_FILE
    graph_path = root / DEFAULT_ARTIFACT_DIR / DEFAULT_GRAPH_FILE
    data = ProjectUIData(
        project_path=root,
        index_path=index_path,
        graph_path=graph_path,
    )

    if not root.exists():
        data.errors.append(f"Project path does not exist: {root}")
        return data
    if not root.is_dir():
        data.errors.append(f"Project path is not a directory: {root}")
        return data

    if index_path.exists():
        try:
            data.index = load_index(index_path)
        except (OSError, ValueError, KeyError) as exc:
            data.errors.append(f"Failed to load index: {type(exc).__name__}: {exc}")
    else:
        data.errors.append(f"Missing index: {index_path}")

    if graph_path.exists():
        try:
            data.graph = load_graph(graph_path)
        except (OSError, ValueError, KeyError) as exc:
            data.errors.append(f"Failed to load graph: {type(exc).__name__}: {exc}")
    else:
        data.errors.append(f"Missing graph: {graph_path}")

    try:
        data.memories = MemoryStore(root).list_memories()
    except (OSError, PermissionError, ValueError) as exc:
        data.errors.append(f"Failed to load memories: {type(exc).__name__}: {exc}")

    try:
        data.tasks = TaskDAGStore(root).list_tasks()
    except (OSError, PermissionError, ValueError) as exc:
        data.errors.append(f"Failed to load tasks: {type(exc).__name__}: {exc}")

    return data


def build_project_overview(data: ProjectUIData) -> dict[str, Any]:
    index = data.index
    graph = data.graph
    return {
        "project_path": str(data.project_path),
        "index_path": str(data.index_path),
        "graph_path": str(data.graph_path),
        "has_index": index is not None,
        "has_graph": graph is not None,
        "python_files": len(index.files) if index else 0,
        "imports": sum(len(file.imports) for file in index.files) if index else 0,
        "classes": sum(len(file.classes) for file in index.files) if index else 0,
        "functions": sum(len(file.functions) for file in index.files) if index else 0,
        "graph_nodes": len(graph.nodes) if graph else 0,
        "graph_edges": len(graph.edges) if graph else 0,
        "memories": len(data.memories),
        "tasks": len(data.tasks),
        "errors": list(data.errors),
    }


def build_file_tree_rows(index: ProjectIndex | None) -> list[dict[str, Any]]:
    if index is None:
        return []
    rows: list[dict[str, Any]] = []
    for file_info in sorted(index.files, key=lambda item: item.path):
        rows.append(
            {
                "path": file_info.path,
                "imports": len(file_info.imports),
                "classes": len(file_info.classes),
                "functions": len(file_info.functions),
                "has_main_guard": file_info.has_main_guard,
            }
        )
    return rows


def build_graph_node_rows(graph: CodeGraph | None) -> list[dict[str, Any]]:
    if graph is None:
        return []
    return [
        {
            "id": node.id,
            "type": node.type,
            "name": node.name,
            "path": node.path or "",
        }
        for node in graph.nodes
    ]


def build_graph_edge_rows(graph: CodeGraph | None) -> list[dict[str, Any]]:
    if graph is None:
        return []
    return [_edge_row(edge) for edge in graph.edges]


def build_memory_rows(memories: list[MemoryIndexEntry]) -> list[dict[str, Any]]:
    return [
        {
            "name": memory.name,
            "type": memory.type,
            "description": memory.description,
            "path": memory.path,
            "tags": ", ".join(memory.tags),
        }
        for memory in memories
    ]


def build_task_rows(tasks: list[TaskNode]) -> list[dict[str, Any]]:
    return [
        {
            "id": task.id,
            "title": task.title,
            "status": task.status,
            "owner": task.owner or "",
            "blocked_by": ", ".join(task.blocked_by),
            "updated_at": task.updated_at,
        }
        for task in tasks
    ]


def graph_edge_type_counts(graph: CodeGraph | None) -> dict[str, int]:
    if graph is None:
        return {}
    return count_by_type(graph.edges)


def graph_node_type_counts(graph: CodeGraph | None) -> dict[str, int]:
    if graph is None:
        return {}
    return count_by_type(graph.nodes)


def _edge_row(edge: GraphEdge) -> dict[str, str]:
    return {
        "source": edge.source,
        "type": edge.type,
        "target": edge.target,
    }
