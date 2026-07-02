import argparse
import sys
from pathlib import Path

from pycode.agent import AgentResult, run_agent_task
from pycode.graph_builder import build_code_graph
from pycode.llm_client import DEFAULT_MODEL, LLMClient, OpenAIResponsesClient
from pycode.models import CodeGraph, GraphEdge, GraphNode, ProjectIndex
from pycode.prompt_builder import build_code_qa_prompt
from pycode.parser import parse_python_file
from pycode.query import (
    find_entry_candidates,
    get_file_imported_by,
    get_file_imports,
    get_function_calls,
)
from pycode.retriever import (
    RetrievalResult,
    retrieve_explain,
    retrieve_for_question,
    retrieve_impact,
    retrieve_onboard,
)
from pycode.scanner import scan_python_files
from pycode.storage import load_graph, load_index, save_graph, save_index
from pycode.tools import ToolSpec


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


def ask_project(
    project_path: Path,
    question: str,
    model: str | None = None,
    llm_client: LLMClient | None = None,
) -> str:
    """Answer a natural-language question using selected project context."""
    index, graph = _load_project_artifacts(project_path)
    retrieval = retrieve_for_question(question, project_path, index, graph)
    return _answer_with_retrieval(retrieval, model, llm_client)


def explain_project_target(
    project_path: Path,
    file_path: str,
    model: str | None = None,
    llm_client: LLMClient | None = None,
) -> str:
    """Explain one project file using selected context."""
    index, graph = _load_project_artifacts(project_path)
    retrieval = retrieve_explain(file_path, project_path, index, graph)
    return _answer_with_retrieval(retrieval, model, llm_client)


def onboard_project(
    project_path: Path,
    model: str | None = None,
    llm_client: LLMClient | None = None,
) -> str:
    """Generate a newcomer reading order from project graph context."""
    index, graph = _load_project_artifacts(project_path)
    retrieval = retrieve_onboard(project_path, index, graph)
    return _answer_with_retrieval(retrieval, model, llm_client)


def impact_project_target(
    project_path: Path,
    file_path: str,
    model: str | None = None,
    llm_client: LLMClient | None = None,
) -> str:
    """Analyze the likely impact of changing one file."""
    index, graph = _load_project_artifacts(project_path)
    retrieval = retrieve_impact(file_path, project_path, index, graph)
    return _answer_with_retrieval(retrieval, model, llm_client)


def agent_project(
    project_path: Path,
    task: str,
    *,
    run_tests: bool = False,
    plan_only: bool = False,
    model: str | None = None,
    graph_path: Path | None = None,
    llm_client: LLMClient | None = None,
    tools: dict[str, ToolSpec] | None = None,
) -> AgentResult:
    """Run the stage-4 Agent workflow for a development-analysis task."""
    client = None if plan_only else llm_client or OpenAIResponsesClient(model=model)
    result = run_agent_task(
        task,
        project_path,
        allow_tests=run_tests,
        graph_path=_resolve_agent_graph_path(project_path, graph_path),
        llm_client=client,
        tools=tools,
        plan_only=plan_only,
    )
    _print_agent_result(result)
    return result


def _resolve_agent_graph_path(
    project_path: Path,
    graph_path: Path | None,
) -> Path | None:
    if graph_path is None:
        return None
    if graph_path.is_absolute():
        return graph_path
    if graph_path.exists():
        return graph_path.resolve()

    project_relative = project_path / graph_path
    if project_relative.exists():
        return project_relative.resolve()
    return graph_path


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


def _load_project_artifacts(project_path: Path) -> tuple[ProjectIndex, CodeGraph]:
    index_path = project_path / DEFAULT_INDEX_DIR / DEFAULT_INDEX_FILE
    graph_path = project_path / DEFAULT_INDEX_DIR / DEFAULT_GRAPH_FILE
    missing: list[str] = []
    if not index_path.exists():
        missing.append(str(index_path))
    if not graph_path.exists():
        missing.append(str(graph_path))
    if missing:
        raise FileNotFoundError(
            "Missing PyCode artifacts: "
            + ", ".join(missing)
            + ". Run `pycode index <project_path>` and `pycode graph <project_path>` first."
        )
    return load_index(index_path), load_graph(graph_path)


def _answer_with_retrieval(
    retrieval: RetrievalResult,
    model: str,
    llm_client: LLMClient | None,
) -> str:
    prompt = build_code_qa_prompt(retrieval)
    client = llm_client or OpenAIResponsesClient(model=model)
    answer = client.generate(prompt)
    _print_llm_answer(answer, retrieval)
    return answer


def _print_llm_answer(answer: str, retrieval: RetrievalResult) -> None:
    print("PyCode answer completed.")
    print(f"Intent: {retrieval.intent}")
    print("Answer:")
    _safe_print(answer)
    print("Evidence:")
    evidence = retrieval.evidence
    if not evidence:
        print("- N/A")
        return
    for item in evidence:
        _safe_print(f"- {item}")


def _print_agent_result(result: AgentResult) -> None:
    print("PyCode agent completed.")
    print(f"Task: {result.task.description}")
    print(f"Task type: {result.task.task_type}")
    print(f"Project path: {result.task.project_path}")
    print(f"Tests allowed: {result.task.allow_tests}")
    print(f"Stop reason: {result.stop_reason}")
    print(f"Runtime turns: {len(result.turns)}")
    print(f"Steps: {len(result.steps)}")

    for index, step in enumerate(result.steps, start=1):
        if index <= len(result.tool_results):
            tool_result = result.tool_results[index - 1]
            status = "ok" if tool_result.ok else "failed"
            print(f"{index}. {step.tool}: {status} - {tool_result.summary}")
            if tool_result.error:
                _safe_print(f"   Error: {tool_result.error}")
            continue
        print(f"{index}. {step.tool}: planned - {step.reason or 'N/A'}")
        if step.arguments:
            _safe_print(f"   Arguments: {step.arguments}")

    if result.turns:
        print("Runtime:")
        for turn in result.turns:
            status = "ok" if turn.tool_result.ok else "failed"
            _safe_print(
                f"- turn {turn.index}: {turn.tool_call.name} -> "
                f"{status} - {turn.tool_result.summary}"
            )

    print("Evidence:")
    evidence = _agent_evidence(result)
    if not evidence:
        print("- N/A")
    else:
        for item in evidence:
            _safe_print(f"- {item}")

    print("Answer:")
    _safe_print(result.answer or "N/A")


def _agent_evidence(result: AgentResult) -> list[str]:
    evidence: list[str] = []
    for tool_result in result.tool_results:
        data = tool_result.data
        for item in data.get("evidence", []):
            evidence.append(str(item))
        for item in data.get("files", []):
            evidence.append(str(item))
        if data.get("path"):
            evidence.append(str(data["path"]))
        for item in data.get("matches", []):
            path = item.get("path")
            line_number = item.get("line_number")
            if path and line_number:
                evidence.append(f"{path}:{line_number}")
            elif path:
                evidence.append(str(path))
        for item in data.get("items", []):
            path = item.get("path")
            if path:
                evidence.append(str(path))
            evidence.extend(str(node_id) for node_id in item.get("node_ids", []))
            evidence.extend(str(edge) for edge in item.get("edges", []))
        for edge in data.get("edges", []):
            source = edge.get("source")
            edge_type = edge.get("type")
            target = edge.get("target")
            if source and edge_type and target:
                evidence.append(f"{source} --{edge_type}--> {target}")
        for node in data.get("nodes", []):
            node_id = node.get("id")
            if node_id:
                evidence.append(str(node_id))
    return _dedupe(evidence)


def _safe_print(text: str) -> None:
    encoding = sys.stdout.encoding or "utf-8"
    safe_text = text.encode(encoding, errors="replace").decode(encoding)
    print(safe_text)


def _count_by_type(items: list[GraphNode] | list[GraphEdge]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.type] = counts.get(item.type, 0) + 1
    return counts


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


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

    ask_parser = subparsers.add_parser(
        "ask",
        help="Ask a natural-language question about a generated project graph.",
    )
    ask_parser.add_argument(
        "project_path",
        type=Path,
        help="Python project directory containing .pclens/index.json and .pclens/code_graph.json.",
    )
    ask_parser.add_argument(
        "question",
        help="Natural-language question about the project.",
    )
    _add_model_argument(ask_parser)

    explain_parser = subparsers.add_parser(
        "explain",
        help="Explain one project file using indexed context.",
    )
    explain_parser.add_argument(
        "project_path",
        type=Path,
        help="Python project directory containing PyCode artifacts.",
    )
    explain_parser.add_argument(
        "file_path",
        help="Project-relative file path to explain.",
    )
    _add_model_argument(explain_parser)

    onboard_parser = subparsers.add_parser(
        "onboard",
        help="Generate a newcomer reading order for the project.",
    )
    onboard_parser.add_argument(
        "project_path",
        type=Path,
        help="Python project directory containing PyCode artifacts.",
    )
    _add_model_argument(onboard_parser)

    impact_parser = subparsers.add_parser(
        "impact",
        help="Analyze likely impact of changing one file.",
    )
    impact_parser.add_argument(
        "project_path",
        type=Path,
        help="Python project directory containing PyCode artifacts.",
    )
    impact_parser.add_argument(
        "file_path",
        help="Project-relative file path to analyze.",
    )
    _add_model_argument(impact_parser)

    agent_parser = subparsers.add_parser(
        "agent",
        help="Run the stage-4 Agent workflow for a development-analysis task.",
    )
    agent_parser.add_argument(
        "project_path",
        type=Path,
        help="Python project directory to analyze.",
    )
    agent_parser.add_argument(
        "task",
        help="Development-analysis task for the Agent.",
    )
    test_group = agent_parser.add_mutually_exclusive_group()
    test_group.add_argument(
        "--run-tests",
        action="store_true",
        help="Allow the Agent to run controlled pytest commands.",
    )
    test_group.add_argument(
        "--no-tests",
        action="store_true",
        help="Analyze only and do not run tests. This is the default.",
    )
    agent_parser.add_argument(
        "--plan-only",
        action="store_true",
        help="Show the planned Agent tool steps without running tools or calling the LLM.",
    )
    agent_parser.add_argument(
        "--graph",
        dest="graph_path",
        type=Path,
        default=None,
        help="Path to an existing code_graph.json. Defaults to <project>/.pclens/code_graph.json.",
    )
    _add_model_argument(agent_parser)

    return parser


def _add_model_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        default=None,
        help=f"OpenAI model to use. Defaults to OPENAI_MODEL from .env, then {DEFAULT_MODEL}.",
    )


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
    elif args.command == "ask":
        ask_project(args.project_path, args.question, args.model)
    elif args.command == "explain":
        explain_project_target(args.project_path, args.file_path, args.model)
    elif args.command == "onboard":
        onboard_project(args.project_path, args.model)
    elif args.command == "impact":
        impact_project_target(args.project_path, args.file_path, args.model)
    elif args.command == "agent":
        agent_project(
            args.project_path,
            args.task,
            run_tests=args.run_tests,
            plan_only=args.plan_only,
            model=args.model,
            graph_path=args.graph_path,
        )


if __name__ == "__main__":
    main()
