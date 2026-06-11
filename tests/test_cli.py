from pathlib import Path

import pytest

from pycode.cli import build_parser, graph_project, query_project_graph
from pycode.models import GraphEdge, GraphNode


def test_graph_project_builds_and_saves_code_graph(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = _create_sample_project(tmp_path)
    output_path = project_path / ".pclens" / "code_graph.json"

    graph = graph_project(project_path)

    captured = capsys.readouterr()
    assert output_path.exists()
    assert "PyCode graph completed." in captured.out
    assert "Graph file:" in captured.out
    assert GraphNode(
        id="file:main.py",
        type="file",
        name="main.py",
        path="main.py",
    ) in graph.nodes
    assert GraphEdge(
        source="file:main.py",
        target="file:services/user_service.py",
        type="imports",
    ) in graph.edges


def test_query_project_graph_reads_graph_and_returns_imports(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = _create_sample_project(tmp_path)
    graph_project(project_path)
    capsys.readouterr()

    result = query_project_graph(project_path, "imports", "main.py")

    captured = capsys.readouterr()
    assert result == [
        GraphEdge(
            source="file:main.py",
            target="file:services/user_service.py",
            type="imports",
        )
    ]
    assert "PyCode query completed." in captured.out
    assert "file:main.py --imports--> file:services/user_service.py" in captured.out


def test_query_project_graph_returns_entry_candidates_without_target(
    tmp_path: Path,
) -> None:
    project_path = _create_sample_project(tmp_path)
    graph_project(project_path)

    result = query_project_graph(project_path, "entry")

    assert result == [
        GraphNode(
            id="file:main.py",
            type="file",
            name="main.py",
            path="main.py",
        )
    ]


def test_query_project_graph_requires_target_for_targeted_queries(
    tmp_path: Path,
) -> None:
    project_path = _create_sample_project(tmp_path)
    graph_project(project_path)

    with pytest.raises(ValueError, match="requires a target"):
        query_project_graph(project_path, "imports")


def test_build_parser_accepts_stage_two_commands() -> None:
    parser = build_parser()

    graph_args = parser.parse_args(["graph", "demo_project", "-o", "graph.json"])
    query_args = parser.parse_args(["query", "calls", "demo_project", "func:main.py:main"])

    assert graph_args.command == "graph"
    assert graph_args.project_path == Path("demo_project")
    assert graph_args.output_path == Path("graph.json")
    assert query_args.command == "query"
    assert query_args.query_type == "calls"
    assert query_args.target == "func:main.py:main"


def _create_sample_project(tmp_path: Path) -> Path:
    project_path = tmp_path / "demo_project"
    service_dir = project_path / "services"
    service_dir.mkdir(parents=True)

    (project_path / "main.py").write_text(
        "\n".join(
            [
                "from services.user_service import UserService",
                "",
                "def main():",
                "    service = UserService()",
                "    service.get_user()",
                "",
                "if __name__ == '__main__':",
                "    main()",
            ]
        ),
        encoding="utf-8",
    )
    (service_dir / "user_service.py").write_text(
        "\n".join(
            [
                "class UserService:",
                "    def get_user(self):",
                "        return 'alice'",
            ]
        ),
        encoding="utf-8",
    )
    return project_path
