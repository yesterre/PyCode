from pycode.models import CodeGraph, GraphEdge, GraphNode
from pycode.query import (
    find_entry_candidates,
    get_file_imported_by,
    get_file_imports,
    get_function_calls,
    get_source_nodes,
    get_target_nodes,
)


def test_get_file_imports_returns_import_edges_from_file() -> None:
    graph = _sample_graph()

    result = get_file_imports(graph, "main.py")

    assert result == [
        GraphEdge(
            source="file:main.py",
            target="file:services/user_service.py",
            type="imports",
        ),
        GraphEdge(source="file:main.py", target="external:os", type="imports"),
    ]


def test_get_file_imports_accepts_windows_style_path() -> None:
    graph = _sample_graph()

    result = get_file_imports(graph, "services\\user_service.py")

    assert result == [
        GraphEdge(
            source="file:services/user_service.py",
            target="file:models/user.py",
            type="imports",
        )
    ]


def test_get_file_imported_by_returns_reverse_import_edges() -> None:
    graph = _sample_graph()

    result = get_file_imported_by(graph, "services/user_service.py")

    assert result == [
        GraphEdge(
            source="file:main.py",
            target="file:services/user_service.py",
            type="imports",
        )
    ]


def test_get_function_calls_returns_calls_edges_from_function_or_method() -> None:
    graph = _sample_graph()

    result = get_function_calls(graph, "func:main.py:main")

    assert result == [
        GraphEdge(
            source="func:main.py:main",
            target="class:services/user_service.py:UserService",
            type="calls",
        ),
        GraphEdge(
            source="func:main.py:main",
            target="method:main.py:AppRunner.run",
            type="calls",
        ),
    ]


def test_find_entry_candidates_uses_entry_file_names_and_main_functions() -> None:
    graph = _sample_graph()

    result = find_entry_candidates(graph)

    assert result == [
        GraphNode(id="file:main.py", type="file", name="main.py", path="main.py"),
        GraphNode(id="file:app.py", type="file", name="app.py", path="app.py"),
        GraphNode(
            id="file:workers/job.py",
            type="file",
            name="workers/job.py",
            path="workers/job.py",
        ),
    ]


def test_queries_return_empty_lists_for_missing_targets() -> None:
    graph = _sample_graph()

    assert get_file_imports(graph, "missing.py") == []
    assert get_file_imported_by(graph, "missing.py") == []
    assert get_function_calls(graph, "func:missing.py:main") == []


def test_get_target_and_source_nodes_resolve_edges_to_nodes() -> None:
    graph = _sample_graph()
    import_edges = get_file_imports(graph, "main.py")

    assert get_target_nodes(graph, import_edges) == [
        GraphNode(
            id="file:services/user_service.py",
            type="file",
            name="services/user_service.py",
            path="services/user_service.py",
        ),
        GraphNode(id="external:os", type="external", name="os"),
    ]
    assert get_source_nodes(graph, import_edges) == [
        GraphNode(id="file:main.py", type="file", name="main.py", path="main.py"),
        GraphNode(id="file:main.py", type="file", name="main.py", path="main.py"),
    ]


def _sample_graph() -> CodeGraph:
    return CodeGraph(
        project_path="demo_project",
        nodes=[
            GraphNode(id="file:main.py", type="file", name="main.py", path="main.py"),
            GraphNode(
                id="file:services/user_service.py",
                type="file",
                name="services/user_service.py",
                path="services/user_service.py",
            ),
            GraphNode(
                id="file:models/user.py",
                type="file",
                name="models/user.py",
                path="models/user.py",
            ),
            GraphNode(id="file:app.py", type="file", name="app.py", path="app.py"),
            GraphNode(
                id="file:workers/job.py",
                type="file",
                name="workers/job.py",
                path="workers/job.py",
            ),
            GraphNode(id="external:os", type="external", name="os"),
            GraphNode(
                id="func:main.py:main",
                type="function",
                name="main",
                path="main.py",
            ),
            GraphNode(
                id="method:main.py:AppRunner.run",
                type="method",
                name="AppRunner.run",
                path="main.py",
            ),
            GraphNode(
                id="func:workers/job.py:main",
                type="function",
                name="main",
                path="workers/job.py",
            ),
            GraphNode(
                id="class:services/user_service.py:UserService",
                type="class",
                name="UserService",
                path="services/user_service.py",
            ),
        ],
        edges=[
            GraphEdge(
                source="file:main.py",
                target="file:services/user_service.py",
                type="imports",
            ),
            GraphEdge(source="file:main.py", target="external:os", type="imports"),
            GraphEdge(
                source="file:services/user_service.py",
                target="file:models/user.py",
                type="imports",
            ),
            GraphEdge(
                source="file:main.py",
                target="func:main.py:main",
                type="contains",
            ),
            GraphEdge(
                source="file:workers/job.py",
                target="func:workers/job.py:main",
                type="contains",
            ),
            GraphEdge(
                source="func:main.py:main",
                target="class:services/user_service.py:UserService",
                type="calls",
            ),
            GraphEdge(
                source="func:main.py:main",
                target="method:main.py:AppRunner.run",
                type="calls",
            ),
        ],
    )
