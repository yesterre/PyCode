from pycode.graph_builder import build_code_graph
from pycode.models import CallInfo, ClassInfo, FileInfo, GraphEdge, ProjectIndex


def test_build_code_graph_creates_structure_nodes_and_contains_edges() -> None:
    index = ProjectIndex(
        project_path="demo_project",
        files=[
            FileInfo(
                path="main.py",
                classes=[ClassInfo(name="AppRunner", methods=["run"])],
                functions=["main"],
            )
        ],
    )

    graph = build_code_graph(index)

    assert graph.project_path == "demo_project"
    assert _node(graph, "file:main.py").type == "file"
    assert _node(graph, "func:main.py:main").type == "function"
    assert _node(graph, "class:main.py:AppRunner").type == "class"
    assert _node(graph, "method:main.py:AppRunner.run").type == "method"
    assert _has_edge(graph, "file:main.py", "func:main.py:main", "contains")
    assert _has_edge(graph, "file:main.py", "class:main.py:AppRunner", "contains")
    assert _has_edge(
        graph,
        "class:main.py:AppRunner",
        "method:main.py:AppRunner.run",
        "contains",
    )


def test_build_code_graph_resolves_internal_and_external_import_edges() -> None:
    index = ProjectIndex(
        project_path="demo_project",
        files=[
            FileInfo(
                path="main.py",
                imports=["services.user_service.UserService", "os"],
            ),
            FileInfo(
                path="services/user_service.py",
                classes=[ClassInfo(name="UserService")],
            ),
        ],
    )

    graph = build_code_graph(index)

    assert _has_edge(
        graph,
        "file:main.py",
        "file:services/user_service.py",
        "imports",
    )
    assert _node(graph, "external:os").type == "external"
    assert _has_edge(graph, "file:main.py", "external:os", "imports")


def test_build_code_graph_creates_call_edges_for_known_and_unknown_calls() -> None:
    index = ProjectIndex(
        project_path="demo_project",
        files=[
            FileInfo(
                path="main.py",
                functions=["main", "load_config"],
                classes=[ClassInfo(name="AppRunner", methods=["run"])],
                call_infos=[
                    CallInfo(
                        caller="main",
                        calls=["load_config", "AppRunner", "runner.run", "print"],
                    ),
                    CallInfo(caller="AppRunner.run", calls=["load_config"]),
                ],
            )
        ],
    )

    graph = build_code_graph(index)

    assert _has_edge(
        graph,
        "func:main.py:main",
        "func:main.py:load_config",
        "calls",
    )
    assert _has_edge(
        graph,
        "func:main.py:main",
        "class:main.py:AppRunner",
        "calls",
    )
    assert _node(graph, "func:runner.run").type == "function"
    assert _has_edge(graph, "func:main.py:main", "func:runner.run", "calls")
    assert _node(graph, "func:print").type == "function"
    assert _has_edge(graph, "func:main.py:main", "func:print", "calls")
    assert _has_edge(
        graph,
        "method:main.py:AppRunner.run",
        "func:main.py:load_config",
        "calls",
    )


def test_build_code_graph_deduplicates_nodes_and_edges() -> None:
    index = ProjectIndex(
        project_path="demo_project",
        files=[
            FileInfo(
                path="main.py",
                imports=["os", "os"],
                functions=["main"],
                call_infos=[CallInfo(caller="main", calls=["print", "print"])],
            )
        ],
    )

    graph = build_code_graph(index)

    assert [node.id for node in graph.nodes].count("external:os") == 1
    assert [node.id for node in graph.nodes].count("func:print") == 1
    assert graph.edges.count(
        _edge("file:main.py", "external:os", "imports")
    ) == 1
    assert graph.edges.count(
        _edge("func:main.py:main", "func:print", "calls")
    ) == 1


def _node(graph, node_id: str):
    return next(node for node in graph.nodes if node.id == node_id)


def _has_edge(graph, source: str, target: str, edge_type: str) -> bool:
    return _edge(source, target, edge_type) in graph.edges


def _edge(source: str, target: str, edge_type: str) -> GraphEdge:
    return GraphEdge(source=source, target=target, type=edge_type)
