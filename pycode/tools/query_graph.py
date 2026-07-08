from pathlib import Path

from pycode.constants import DEFAULT_ARTIFACT_DIR, DEFAULT_GRAPH_FILE
from pycode.query import (
    find_entry_candidates,
    get_file_imported_by,
    get_file_imports,
    get_function_calls,
    get_source_nodes,
    get_target_nodes,
)
from pycode.storage import load_graph
from pycode.tools.base import ToolContext, failure, success


def query_code_graph(
    context: ToolContext,
    query_type: str,
    target: str | None = None,
    *,
    graph_path: str | Path | None = None,
):
    """Query the stage-2 code graph through a stable tool wrapper."""
    graph_file = graph_path or f"{DEFAULT_ARTIFACT_DIR}/{DEFAULT_GRAPH_FILE}"
    try:
        resolved_graph_path = context.resolve_in_project(graph_file)
    except PermissionError as exc:
        return failure("query_graph", "Graph read denied.", str(exc))

    try:
        graph = load_graph(resolved_graph_path)
    except (FileNotFoundError, IsADirectoryError) as exc:
        return failure("query_graph", "Graph file cannot be loaded.", str(exc))

    if query_type == "imports":
        if target is None:
            return failure("query_graph", "Missing query target.", "imports requires target")
        edges = get_file_imports(graph, target)
        nodes = get_target_nodes(graph, edges)
        return _edge_result("imports", target, edges, nodes)
    if query_type == "imported-by":
        if target is None:
            return failure(
                "query_graph",
                "Missing query target.",
                "imported-by requires target",
            )
        edges = get_file_imported_by(graph, target)
        nodes = get_source_nodes(graph, edges)
        return _edge_result("imported-by", target, edges, nodes)
    if query_type == "calls":
        if target is None:
            return failure("query_graph", "Missing query target.", "calls requires target")
        edges = get_function_calls(graph, target)
        nodes = get_target_nodes(graph, edges)
        return _edge_result("calls", target, edges, nodes)
    if query_type == "entry":
        nodes = find_entry_candidates(graph)
        return success(
            "query_graph",
            f"Found {len(nodes)} entry candidates.",
            query_type=query_type,
            target=target,
            nodes=[_node_data(node) for node in nodes],
            edges=[],
        )

    return failure(
        "query_graph",
        "Unsupported graph query.",
        f"Unsupported query_type: {query_type}",
    )


def _edge_result(query_type, target, edges, nodes):
    return success(
        "query_graph",
        f"Found {len(edges)} {query_type} edges.",
        query_type=query_type,
        target=target,
        edges=[_edge_data(edge) for edge in edges],
        nodes=[_node_data(node) for node in nodes],
    )


def _edge_data(edge):
    return {"source": edge.source, "target": edge.target, "type": edge.type}


def _node_data(node):
    return {"id": node.id, "type": node.type, "name": node.name, "path": node.path}
