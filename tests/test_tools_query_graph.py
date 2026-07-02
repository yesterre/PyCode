from pathlib import Path
import shutil
import uuid

from pycode.models import CodeGraph, GraphEdge, GraphNode
from pycode.storage import save_graph
from pycode.tools import ToolContext, query_code_graph


def test_query_code_graph_returns_import_edges_and_target_nodes() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        graph_path = project_path / ".pclens" / "code_graph.json"
        save_graph(_sample_graph(project_path), graph_path)

        result = query_code_graph(ToolContext(project_path), "imports", "main.py")

        assert result.ok is True
        assert result.data["edges"] == [
            {
                "source": "file:main.py",
                "target": "file:services/user_service.py",
                "type": "imports",
            }
        ]
        assert result.data["nodes"][0]["path"] == "services/user_service.py"
    finally:
        _cleanup(workspace)


def test_query_code_graph_returns_entry_candidates() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        graph_path = project_path / ".pclens" / "code_graph.json"
        save_graph(_sample_graph(project_path), graph_path)

        result = query_code_graph(ToolContext(project_path), "entry")

        assert result.ok is True
        assert result.data["nodes"][0]["id"] == "file:main.py"
    finally:
        _cleanup(workspace)


def test_query_code_graph_reports_missing_graph() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()

        result = query_code_graph(ToolContext(project_path), "entry")

        assert result.ok is False
        assert result.summary == "Graph file cannot be loaded."
    finally:
        _cleanup(workspace)


def _sample_graph(project_path: Path) -> CodeGraph:
    project_path.mkdir(exist_ok=True)
    return CodeGraph(
        project_path=str(project_path),
        nodes=[
            GraphNode(id="file:main.py", type="file", name="main.py", path="main.py"),
            GraphNode(
                id="file:services/user_service.py",
                type="file",
                name="services/user_service.py",
                path="services/user_service.py",
            ),
        ],
        edges=[
            GraphEdge(
                source="file:main.py",
                target="file:services/user_service.py",
                type="imports",
            )
        ],
    )


def _workspace() -> Path:
    path = Path(".pytest_tmp_tools") / f"query_graph_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    return path.resolve()


def _cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
