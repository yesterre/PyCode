from pathlib import Path

from pycode.agent.memory import MemoryStore
from pycode.agent.task_dag import TaskDAGStore
from pycode.models import CodeGraph, FileInfo, GraphEdge, GraphNode, ProjectIndex
from pycode.storage import save_graph, save_index
from ui.data_loader import (
    build_file_tree_rows,
    build_graph_edge_rows,
    build_memory_rows,
    build_project_overview,
    build_task_rows,
    load_project_ui_data,
)


def test_load_project_ui_data_reads_artifacts_memory_and_tasks(tmp_path: Path) -> None:
    project_path = tmp_path / "demo_project"
    project_path.mkdir()
    index = ProjectIndex(
        project_path=str(project_path),
        files=[
            FileInfo(
                path="main.py",
                imports=["services.user_service"],
                functions=["main"],
                has_main_guard=True,
            )
        ],
    )
    graph = CodeGraph(
        project_path=str(project_path),
        nodes=[GraphNode("file:main.py", "file", "main.py", "main.py")],
        edges=[
            GraphEdge(
                "file:main.py",
                "file:services/user_service.py",
                "imports",
            )
        ],
    )
    save_index(index, project_path / ".pclens" / "index.json")
    save_graph(graph, project_path / ".pclens" / "code_graph.json")
    MemoryStore(project_path).add_memory(
        name="Project Entry",
        memory_type="project",
        description="Entry point",
        body="main.py is the entry point.",
    )
    TaskDAGStore(project_path).create_task(
        task_id="task_001",
        title="Build demo",
    )

    data = load_project_ui_data(project_path)
    overview = build_project_overview(data)

    assert data.has_artifacts
    assert data.errors == []
    assert overview["python_files"] == 1
    assert overview["graph_edges"] == 1
    assert overview["memories"] == 1
    assert overview["tasks"] == 1
    assert build_file_tree_rows(data.index)[0]["path"] == "main.py"
    assert build_graph_edge_rows(data.graph)[0]["type"] == "imports"
    assert build_memory_rows(data.memories)[0]["name"] == "project-entry"
    assert build_task_rows(data.tasks)[0]["id"] == "task_001"


def test_load_project_ui_data_reports_missing_artifacts(tmp_path: Path) -> None:
    project_path = tmp_path / "demo_project"
    project_path.mkdir()

    data = load_project_ui_data(project_path)

    assert not data.has_artifacts
    assert any("Missing index" in error for error in data.errors)
    assert any("Missing graph" in error for error in data.errors)
