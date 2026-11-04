"""Bounded public projection of an immutable CodeGraph artifact."""

from uuid import UUID

from backend.app.core.errors import BackendError
from pycode.models import CodeGraph
from pycode.storage import load_graph
from pycode.graph_schema import graph_info, node_data, edge_data, public_sites


def _safe_path(path: str | None) -> bool:
    if path is None:
        return True
    normalized = path.replace("\\", "/")
    parts = normalized.split("/")
    return bool(normalized) and not normalized.startswith("/") and not any(
        part in {"", ".", ".."} for part in parts
    ) and not ":" in normalized


def local_graph(graph: CodeGraph, snapshot_id: UUID, *, q: str | None,
                node_type: str | None, file_path: str | None, limit: int) -> dict:
    nodes = {node.id: node for node in graph.nodes}
    if any(not _safe_path(node.path) for node in graph.nodes):
        raise BackendError("artifacts_unavailable", "Snapshot artifacts are unavailable.")
    if any(edge.source not in nodes or edge.target not in nodes for edge in graph.edges):
        raise BackendError("artifacts_unavailable", "Snapshot artifacts are unavailable.")
    query = q.casefold() if q is not None else None
    seeds = sorted((node for node in graph.nodes if
                    (node_type is None or node.type == node_type) and (
                        (query is not None and (query in node.name.casefold()
                         or query in (node.path or "").casefold()))
                        or (file_path is not None and node.path == file_path)
                    )), key=lambda node: (node.type, node.name.casefold(), node.id))
    selected = {node.id for node in seeds[:min(10, limit)]}
    truncated = len(seeds) > min(10, limit)
    if not selected:
        # Preserve the exact established empty-query response. No graph facts
        # were selected, so absence of metadata must not imply any capability.
        return {"snapshot_id": snapshot_id, "nodes": [], "edges": [], "truncated": False}
    neighbors = sorted({edge.target if edge.source in selected else edge.source
                        for edge in graph.edges
                        if edge.source in selected or edge.target in selected} - selected)
    for node_id in neighbors:
        if len(selected) >= limit:
            truncated = True
            break
        selected.add(node_id)
    ordered_nodes = sorted((nodes[node_id] for node_id in selected), key=lambda node: node.id)
    relevant = sorted((edge for edge in graph.edges
                       if edge.source in selected and edge.target in selected),
                      key=lambda edge: (edge.source, edge.target, edge.type))
    edge_limit = limit * 2
    truncated = truncated or len(relevant) > edge_limit
    sites, omitted = public_sites(graph,selected,min(200,edge_limit))
    return {
        "snapshot_id": snapshot_id,
        "nodes": [node_data(node,details=graph.schema_version in {2,3,4}) for node in ordered_nodes],
        "edges": [edge_data(edge,details=graph.schema_version in {2,3,4}) for edge in relevant[:edge_limit]],
        "truncated": truncated,
        **graph_info(graph), 'call_sites':sites, 'call_sites_omitted':omitted,
    }
