from pathlib import Path
from dataclasses import asdict

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
from pycode.graph_schema import graph_info, public_sites, node_data, edge_data
from pycode.tools.base import ToolContext, failure, success


def query_code_graph(
    context: ToolContext,
    query_type: str,
    target: str | None = None,
    *,
    graph_path: str | Path | None = None,
):
    """Query the stage-2 code graph through a stable tool wrapper."""
    trusted = context.retrieval_context
    if trusted is not None:
        if graph_path is not None or trusted.root != context.project_root:
            return failure("query_graph", "Graph read denied.", "trusted_artifacts_cannot_be_overridden")
        graph = trusted.graph
        if graph is None:
            return failure("query_graph", "Graph file cannot be loaded.", "graph_missing")
    else:
        graph_file = graph_path or f"{DEFAULT_ARTIFACT_DIR}/{DEFAULT_GRAPH_FILE}"
        try:
            resolved_graph_path = context.resolve_in_project(graph_file)
        except PermissionError as exc:
            return failure("query_graph", "Graph read denied.", str(exc))
        try:
            graph = load_graph(resolved_graph_path)
        except (FileNotFoundError, IsADirectoryError) as exc:
            return failure("query_graph", "Graph file cannot be loaded.", str(exc))

    result = _query_graph(graph, query_type, target)
    result.data.update(graph_info(graph))
    ids = {n['id'] for n in result.data.get('nodes',[])}
    if target:
        ids.add(target)
    sites, omitted = public_sites(graph,ids)
    result.data.update(call_sites=sites,call_sites_omitted=omitted)
    metadata = {'graph_schema_version':graph.schema_version,
                'graph_capabilities':list(graph.capabilities) if graph.schema_version==2 else ['legacy_structure'],
                'binding':asdict(trusted.binding) if trusted is not None and trusted.binding else graph.binding}
    if graph.schema_version==2:
        metadata.update(call_sites_count=len(graph.call_sites),
                        unknown_count=sum(s['resolution']=='unknown' for s in graph.call_sites),
                        ambiguous_count=sum(s['resolution']=='ambiguous' for s in graph.call_sites))
    result.data['graph'] = metadata
    if trusted is not None:
        result.data["binding"] = asdict(trusted.binding) if trusted.binding else None
        result.data["relation_status"] = "stored_static_resolution_unverified"
    return result


def _query_graph(graph, query_type, target):

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
        from pycode.query import find_definitions
        matches=find_definitions(graph,target)
        if len(matches)>1:
            return success('query_graph','Symbol address is ambiguous.',query_type=query_type,target=target,
                           nodes=[_node_data(n) for n in matches[:8]],edges=[],resolution='ambiguous',
                           candidate_count=len(matches),candidates_omitted=max(0,len(matches)-8))
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
    unresolved=sum(edge.type=='unresolved_call' for edge in edges)
    summary=f"Found {len(edges)} {query_type} edges."
    if unresolved:
        summary+=f' {unresolved} call targets remain unknown or ambiguous.'
    return success(
        "query_graph",
        summary,
        query_type=query_type,
        target=target,
        edges=[_edge_data(edge) for edge in edges],
        nodes=[_node_data(node) for node in nodes],
    )


def _edge_data(edge):
    return edge_data(edge,details=edge.resolution is not None)


def _node_data(node):
    return node_data(node,details=node.symbol_key is not None)
