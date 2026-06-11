import json
from pathlib import Path

import pytest

from pycode.models import (
    CallInfo,
    ClassInfo,
    CodeGraph,
    FileInfo,
    GraphEdge,
    GraphNode,
    ProjectIndex,
)
from pycode.storage import load_graph, load_index, save_graph, save_index


def test_save_index_creates_parent_directory_and_writes_json(tmp_path: Path) -> None:
    index = ProjectIndex(
        project_path="demo_project",
        files=[
            FileInfo(
                path="main.py",
                imports=["os", "pathlib.Path"],
                classes=[ClassInfo(name="UserService", methods=["get_user"])],
                functions=["main"],
                call_infos=[CallInfo(caller="main", calls=["UserService"])],
                has_main_guard=True,
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
                "call_infos": [
                    {
                        "caller": "main",
                        "calls": ["UserService"],
                    }
                ],
                "has_main_guard": True,
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
    assert result.files[0].call_infos == []
    assert result.files[0].has_main_guard is False


def test_load_index_rebuilds_call_info_and_main_guard(tmp_path: Path) -> None:
    index_path = tmp_path / "index.json"
    index_path.write_text(
        json.dumps(
            {
                "project_path": "demo_project",
                "files": [
                    {
                        "path": "main.py",
                        "imports": [],
                        "classes": [],
                        "functions": ["main"],
                        "call_infos": [
                            {
                                "caller": "main",
                                "calls": ["load_config", "runner.run"],
                            }
                        ],
                        "has_main_guard": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = load_index(index_path)

    assert result.files[0].call_infos == [
        CallInfo(caller="main", calls=["load_config", "runner.run"])
    ]
    assert result.files[0].has_main_guard is True


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


def test_save_graph_creates_parent_directory_and_writes_json(tmp_path: Path) -> None:
    graph = CodeGraph(
        project_path="demo_project",
        nodes=[
            GraphNode(id="file:main.py", type="file", name="main.py", path="main.py"),
            GraphNode(
                id="func:main.py:main",
                type="function",
                name="main",
                path="main.py",
            ),
        ],
        edges=[
            GraphEdge(
                source="file:main.py",
                target="func:main.py:main",
                type="contains",
            )
        ],
    )
    output_path = tmp_path / ".pclens" / "code_graph.json"

    save_graph(graph, output_path)

    assert output_path.exists()
    data = json.loads(output_path.read_text(encoding="utf-8"))
    assert data == {
        "project_path": "demo_project",
        "nodes": [
            {
                "id": "file:main.py",
                "type": "file",
                "name": "main.py",
                "path": "main.py",
            },
            {
                "id": "func:main.py:main",
                "type": "function",
                "name": "main",
                "path": "main.py",
            },
        ],
        "edges": [
            {
                "source": "file:main.py",
                "target": "func:main.py:main",
                "type": "contains",
            }
        ],
    }


def test_load_graph_rebuilds_code_graph_dataclasses(tmp_path: Path) -> None:
    graph_path = tmp_path / "code_graph.json"
    graph_path.write_text(
        json.dumps(
            {
                "project_path": "demo_project",
                "nodes": [
                    {
                        "id": "file:main.py",
                        "type": "file",
                        "name": "main.py",
                        "path": "main.py",
                    },
                    {
                        "id": "external:os",
                        "type": "external",
                        "name": "os",
                        "path": None,
                    },
                ],
                "edges": [
                    {
                        "source": "file:main.py",
                        "target": "external:os",
                        "type": "imports",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = load_graph(graph_path)

    assert result == CodeGraph(
        project_path="demo_project",
        nodes=[
            GraphNode(id="file:main.py", type="file", name="main.py", path="main.py"),
            GraphNode(id="external:os", type="external", name="os", path=None),
        ],
        edges=[
            GraphEdge(source="file:main.py", target="external:os", type="imports")
        ],
    )


def test_load_graph_supports_utf8_bom(tmp_path: Path) -> None:
    graph_path = tmp_path / "code_graph.json"
    graph_path.write_text(
        '\ufeff{"project_path": "demo_project", "nodes": [], "edges": []}',
        encoding="utf-8",
    )

    result = load_graph(graph_path)

    assert result == CodeGraph(project_path="demo_project")


def test_load_graph_raises_for_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_graph(tmp_path / "missing-code-graph.json")


def test_load_graph_raises_for_directory(tmp_path: Path) -> None:
    with pytest.raises(IsADirectoryError):
        load_graph(tmp_path)
