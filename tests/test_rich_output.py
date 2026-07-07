from io import StringIO
from pathlib import Path

import pytest

from pycode.models import CodeGraph, FileInfo, GraphEdge, GraphNode, ProjectIndex
from pycode.rich_output import (
    make_console,
    print_graph_summary_rich,
    print_index_summary_rich,
    print_query_result_rich,
    rich_available,
)


pytest.importorskip("rich")


def test_rich_index_summary_prints_project_metrics() -> None:
    index = ProjectIndex(
        project_path="demo_project",
        files=[
            FileInfo(
                path="main.py",
                imports=["services.user_service"],
                functions=["main"],
            )
        ],
    )
    output = StringIO()

    assert print_index_summary_rich(
        index,
        Path(".pclens/index.json"),
        console=make_console(output),
    )

    text = output.getvalue()
    assert rich_available()
    assert "PyCode Index Completed" in text
    assert "Python files" in text
    assert "main.py" in text


def test_rich_graph_and_query_output_prints_edges() -> None:
    graph = CodeGraph(
        project_path="demo_project",
        nodes=[
            GraphNode("file:main.py", "file", "main.py", "main.py"),
            GraphNode(
                "file:services/user_service.py",
                "file",
                "user_service.py",
                "services/user_service.py",
            ),
        ],
        edges=[
            GraphEdge(
                "file:main.py",
                "file:services/user_service.py",
                "imports",
            )
        ],
    )
    output = StringIO()
    console = make_console(output)

    assert print_graph_summary_rich(graph, Path(".pclens/code_graph.json"), console=console)
    assert print_query_result_rich(
        "imports",
        graph.edges,
        Path(".pclens/code_graph.json"),
        console=console,
    )

    text = output.getvalue()
    assert "PyCode Graph Completed" in text
    assert "PyCode Query Completed" in text
    assert "file:main.py" in text
    assert "file:services/user_service.py" in text
