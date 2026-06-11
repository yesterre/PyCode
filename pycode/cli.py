import argparse
from pathlib import Path

from pycode.graph_builder import build_code_graph
from pycode.models import CodeGraph, GraphEdge, GraphNode, ProjectIndex
from pycode.parser import parse_python_file
from pycode.query import (
    find_entry_candidates,
    get_file_imported_by,
    get_file_imports,
    get_function_calls,
)
from pycode.scanner import scan_python_files
from pycode.storage import load_graph, save_graph, save_index


DEFAULT_INDEX_DIR = ".pclens"
DEFAULT_INDEX_FILE = "index.json"
DEFAULT_GRAPH_FILE = "code_graph.json"


def index_project(
    project_path: Path,
    output_path: Path | None = None,
) -> ProjectIndex:
    """Scan a Python project, parse file structures, and save an index file."""
    if output_path is None:
        output_path = project_path / DEFAULT_INDEX_DIR / DEFAULT_INDEX_FILE

    project_index = build_project_index(project_path)
    save_index(project_index, output_path)
    _print_index_summary(project_index, output_path)
    return project_index


def build_project_index(project_path: Path) -> ProjectIndex:
    """Scan and parse a Python project without writing files."""
    python_files = scan_python_files(project_path)
    file_infos = [
        parse_python_file(file_path, project_path)
        for file_path in python_files
    ]
    return ProjectIndex(
        project_path=str(project_path),
        files=file_infos,
    )


def graph_project(
    project_path: Path,
    output_path: Path | None = None,
) -> CodeGraph:
    """Build and save a code graph for a Python project."""
    if output_path is None:
        output_path = project_path / DEFAULT_INDEX_DIR / DEFAULT_GRAPH_FILE

    project_index = build_project_index(project_path)
    graph = build_code_graph(project_index)
    save_graph(graph, output_path)
    _print_graph_summary(graph, output_path)
    return graph


def query_project_graph(
    project_path: Path,
    query_type: str,
    target: str | None = None,
    graph_path: Path | None = None,
) -> list[GraphEdge] | list[GraphNode]:
    """Load a saved code graph and run a graph query."""
    if graph_path is None:
        graph_path = project_path / DEFAULT_INDEX_DIR / DEFAULT_GRAPH_FILE

    graph = load_graph(graph_path)
    if query_type == "imports":
        _require_target(query_type, target)
        result = get_file_imports(graph, target)
    elif query_type == "imported-by":
        _require_target(query_type, target)
        result = get_file_imported_by(graph, target)
    elif query_type == "calls":
        _require_target(query_type, target)
        result = get_function_calls(graph, target)
    elif query_type == "entry":
        result = find_entry_candidates(graph)
    else:
        raise ValueError(f"Unsupported query type: {query_type}")

    _print_query_result(query_type, result, graph_path)
    return result


def _print_index_summary(index: ProjectIndex, output_path: Path) -> None:
    import_count = sum(len(file.imports) for file in index.files)
    class_count = sum(len(file.classes) for file in index.files)
    function_count = sum(len(file.functions) for file in index.files)

    print("PyCode index completed.")
    print(f"Project path: {index.project_path}")
    print(f"Python files: {len(index.files)}")
    print(f"Imports: {import_count}")
    print(f"Classes: {class_count}")
    print(f"Functions: {function_count}")
    print(f"Index file: {output_path}")


def _print_graph_summary(graph: CodeGraph, output_path: Path) -> None:
    node_counts = _count_by_type(graph.nodes)
    edge_counts = _count_by_type(graph.edges)

    print("PyCode graph completed.")
    print(f"Project path: {graph.project_path}")
    print(f"Nodes: {len(graph.nodes)}")
    print(f"Edges: {len(graph.edges)}")
    print(f"File nodes: {node_counts.get('file', 0)}")
    print(f"Class nodes: {node_counts.get('class', 0)}")
    print(f"Function nodes: {node_counts.get('function', 0)}")
    print(f"Method nodes: {node_counts.get('method', 0)}")
    print(f"Import edges: {edge_counts.get('imports', 0)}")
    print(f"Call edges: {edge_counts.get('calls', 0)}")
    print(f"Graph file: {output_path}")


def _print_query_result(
    query_type: str,
    result: list[GraphEdge] | list[GraphNode],
    graph_path: Path,
) -> None:
    print("PyCode query completed.")
    print(f"Graph file: {graph_path}")
    print(f"Query: {query_type}")
    print(f"Results: {len(result)}")

    if not result:
        return

    for item in result:
        if isinstance(item, GraphEdge):
            print(f"{item.source} --{item.type}--> {item.target}")
        else:
            print(f"{item.id} ({item.type})")


def _count_by_type(items: list[GraphNode] | list[GraphEdge]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.type] = counts.get(item.type, 0) + 1
    return counts


def _require_target(query_type: str, target: str | None) -> None:
    if target is None:
        raise ValueError(f"Query '{query_type}' requires a target argument.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pycode",
        description="PyCode: Python code structure indexing tool.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser(
        "index",
        help="Scan a Python project and generate index.json.",
    )
    index_parser.add_argument(
        "project_path",
        type=Path,
        help="Python project directory to index.",
    )
    index_parser.add_argument(
        "--output",
        "-o",
        dest="output_path",
        type=Path,
        default=None,
        help="Path to write the generated index JSON. Defaults to <project>/.pclens/index.json.",
    )

    graph_parser = subparsers.add_parser(
        "graph",
        help="Scan a Python project and generate code_graph.json.",
    )
    graph_parser.add_argument(
        "project_path",
        type=Path,
        help="Python project directory to analyze.",
    )
    graph_parser.add_argument(
        "--output",
        "-o",
        dest="output_path",
        type=Path,
        default=None,
        help="Path to write the generated graph JSON. Defaults to <project>/.pclens/code_graph.json.",
    )

    query_parser = subparsers.add_parser(
        "query",
        help="Query a generated code graph.",
    )
    query_parser.add_argument(
        "query_type",
        choices=["imports", "imported-by", "calls", "entry"],
        help="Graph query to run.",
    )
    query_parser.add_argument(
        "project_path",
        type=Path,
        help="Python project directory containing .pclens/code_graph.json.",
    )
    query_parser.add_argument(
        "target",
        nargs="?",
        help="File path for imports/imported-by, or function id for calls.",
    )
    query_parser.add_argument(
        "--graph",
        dest="graph_path",
        type=Path,
        default=None,
        help="Path to an existing code_graph.json. Defaults to <project>/.pclens/code_graph.json.",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "index":
        index_project(args.project_path, args.output_path)
    elif args.command == "graph":
        graph_project(args.project_path, args.output_path)
    elif args.command == "query":
        query_project_graph(
            args.project_path,
            args.query_type,
            args.target,
            args.graph_path,
        )


if __name__ == "__main__":
    main()
