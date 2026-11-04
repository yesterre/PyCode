from pycode.models import CodeGraph, GraphEdge, GraphNode
from pycode.utils import normalize_path
from collections import deque
from dataclasses import dataclass


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
    matches = find_definitions(graph,function_id)
    if len(matches)==1:
        function_id=matches[0].id
    return [
        edge
        for edge in graph.edges
        if edge.source == function_id and edge.type in {"calls", "unresolved_call"}
    ]


def find_definitions(graph: CodeGraph, selector: str) -> list[GraphNode]:
    """Resolve an address exactly, or return ALL versioned duplicate addresses."""
    exact=[n for n in graph.nodes if n.id==selector]
    if exact or graph.schema_version==1:
        return exact
    return [n for n in graph.nodes if n.type in {'function','class','method'} and n.id.startswith(selector+'@')]


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
    return f"file:{normalize_path(file_path)}"


def _looks_like_entry_file(node: GraphNode) -> bool:
    path = normalize_path(node.path or node.name).lower()
    name = path.rsplit("/", 1)[-1]
    return name in {"main.py", "app.py", "cli.py", "__main__.py"}


def _is_main_function_node(graph: CodeGraph, node_id: str) -> bool:
    nodes_by_id = _nodes_by_id(graph)
    node = nodes_by_id.get(node_id)
    return node is not None and node.type == "function" and node.name == "main"


def _nodes_by_id(graph: CodeGraph) -> dict[str, GraphNode]:
    return {node.id: node for node in graph.nodes}


@dataclass(frozen=True)
class RetrievalGraph:
    """Call-scoped adjacency built once when loading the trusted Graph.

    Loading sorts V/E once; traversal below separately bounds every inspected
    adjacency entry, including unknown relations and repeated/cyclic entries.
    """
    graph: CodeGraph
    nodes: dict
    adjacency: dict
    symbols: dict

    @classmethod
    def create(cls, graph):
        nodes = {n.id: n for n in sorted(graph.nodes, key=lambda n: n.id)}
        adjacency, symbols = {}, {}
        for n in nodes.values():
            symbols.setdefault((n.path, n.name), []).append(n.id)
            if n.symbol_key is not None:
                symbols.setdefault((n.path, n.symbol_key), []).append(n.id)
        for e in sorted(graph.edges, key=lambda e: (e.source, e.target, e.type, e.site_id or '')):
            for current, other, direction in ((e.source, e.target, 'outgoing'), (e.target, e.source, 'incoming')):
                adjacency.setdefault(current, []).append((other, e, direction))
        return cls(graph, nodes, adjacency, symbols)

    def expand(self, seeds, *, depth, node_limit, edge_limit, direction):
        if (type(depth) is not int or not 0 <= depth <= 4 or type(node_limit) is not int
                or not 1 <= node_limit <= 256 or type(edge_limit) is not int
                or not 1 <= edge_limit <= 4096 or direction not in {'both','incoming','outgoing'}):
            raise ValueError('Invalid Graph traversal bounds')
        initial = sorted(set(seeds))[:node_limit]
        visited = set(initial)
        queue = deque((node, ()) for node in initial)
        hits, checks, unknown, limited, max_depth = [], 0, 0, False, 0
        while queue:
            current, path = queue.popleft()
            if len(path) >= depth:
                continue
            for other, edge, way in self.adjacency.get(current, ()):
                if checks >= edge_limit:
                    limited = True
                    break
                checks += 1
                if direction != 'both' and direction != way:
                    continue
                if edge.type not in {'imports','calls','contains'}:
                    unknown += 1
                    continue
                if self.graph.schema_version==2 and edge.type=='calls' and edge.resolution!='static':
                    unknown += 1
                    continue
                node = self.nodes.get(other)
                if node is None or node.path is None:
                    unknown += 1
                    continue
                if other in visited:
                    continue
                if len(visited) >= node_limit:
                    limited = True
                    break
                visited.add(other)
                step = {'source': edge.source, 'target': edge.target, 'relation': edge.type, 'direction': way}
                if self.graph.schema_version==2:
                    step.update({k:getattr(edge,k) for k in ('resolution','basis','site_id','path','start_line','end_line')
                                 if getattr(edge,k) is not None})
                new_path = (*path, step)
                max_depth = max(max_depth, len(new_path))
                hits.append((node, new_path))
                queue.append((other, new_path))
            if limited:
                break
        return hits, {'visited_nodes':len(visited), 'edge_checks':checks, 'depth_reached':max_depth,
                      'unknown_relations':unknown, 'limited':limited,
                      'node_limit':node_limit, 'edge_limit':edge_limit, 'depth_limit':depth,
                      'indexed_edges':len(self.graph.edges)}
