from pycode.models import CodeGraph, GraphEdge, GraphNode


def get_file_imports(graph: CodeGraph, file_path: str) -> list[GraphEdge]:
    """Return imports edges from the given file."""
    file_id = _file_id(file_path)
    return [
        edge
        for edge in graph.edges
        if edge.source == file_id and edge.type == "imports"
    ]


def get_file_imported_by(graph: CodeGraph, file_path: str) -> list[GraphEdge]:
    """Return imports edges targeting the given file."""
    file_id = _file_id(file_path)
    return [
        edge
        for edge in graph.edges
        if edge.target == file_id and edge.type == "imports"
    ]


def get_function_calls(graph: CodeGraph, function_id: str) -> list[GraphEdge]:
    """Return calls edges from the given function or method node."""
    return [
        edge
        for edge in graph.edges
        if edge.source == function_id and edge.type == "calls"
    ]


def find_entry_candidates(graph: CodeGraph) -> list[GraphNode]:
    """Find likely entry files from graph structure."""
    file_nodes = [node for node in graph.nodes if node.type == "file"]
    candidate_ids: set[str] = set()

    for node in file_nodes:
        if _looks_like_entry_file(node):
            candidate_ids.add(node.id)

    main_function_file_ids = {
        edge.source
        for edge in graph.edges
        if edge.type == "contains" and _is_main_function_node(graph, edge.target)
    }
    candidate_ids.update(main_function_file_ids)

    return [node for node in file_nodes if node.id in candidate_ids]


def get_target_nodes(graph: CodeGraph, edges: list[GraphEdge]) -> list[GraphNode]:
    """Return target nodes for a list of graph edges."""
    nodes_by_id = _nodes_by_id(graph)
    return [
        nodes_by_id[edge.target]
        for edge in edges
        if edge.target in nodes_by_id
    ]


def get_source_nodes(graph: CodeGraph, edges: list[GraphEdge]) -> list[GraphNode]:
    """Return source nodes for a list of graph edges."""
    nodes_by_id = _nodes_by_id(graph)
    return [
        nodes_by_id[edge.source]
        for edge in edges
        if edge.source in nodes_by_id
    ]


def _file_id(file_path: str) -> str:
    return f"file:{_normalize_path(file_path)}"


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/")


def _looks_like_entry_file(node: GraphNode) -> bool:
    path = _normalize_path(node.path or node.name).lower()
    name = path.rsplit("/", 1)[-1]
    return name in {"main.py", "app.py", "cli.py", "__main__.py"}


def _is_main_function_node(graph: CodeGraph, node_id: str) -> bool:
    nodes_by_id = _nodes_by_id(graph)
    node = nodes_by_id.get(node_id)
    return node is not None and node.type == "function" and node.name == "main"


def _nodes_by_id(graph: CodeGraph) -> dict[str, GraphNode]:
    return {node.id: node for node in graph.nodes}
