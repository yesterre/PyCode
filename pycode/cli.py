import argparse
import sys
from pathlib import Path

from pycode.agent import AgentResult, run_agent_task
from pycode.agent.memory import MemoryIndexEntry, MemoryItem, MemoryStore
from pycode.agent.task_dag import TaskDAGStore, TaskNode
from pycode.graph_builder import build_code_graph
from pycode.llm_client import LLMClient, OpenAIResponsesClient
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
from pycode.utils import dedupe_preserve_order


DEFAULT_INDEX_DIR = ".pclens"
DEFAULT_INDEX_FILE = "index.json"
DEFAULT_GRAPH_FILE = "code_graph.json"
_CONFIGURED_STDOUT_ID: int | None = None


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
    enable_memory: bool = True,
    enable_memory_extraction: bool = True,
    show_context: bool = False,
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
        enable_memory=enable_memory,
        enable_memory_extraction=enable_memory_extraction,
    )
    _print_agent_result(result, show_context=show_context)
    return result


def memory_project(
    project_path: Path,
    operation: str,
    *,
    name: str | None = None,
    memory_type: str | None = None,
    description: str = "",
    body: str | None = None,
    tags: list[str] | None = None,
    query: str = "",
    limit: int = 5,
) -> list[MemoryIndexEntry] | list[MemoryItem] | MemoryItem:
    """Manage project-local persistent memory files under .pclens/memory."""
    store = MemoryStore(project_path)

    if operation == "add":
        if not name:
            raise ValueError("Memory operation 'add' requires --name.")
        if not memory_type:
            raise ValueError("Memory operation 'add' requires --type.")
        if body is None:
            raise ValueError("Memory operation 'add' requires --content.")
        item = store.add_memory(
            name=name,
            memory_type=memory_type,
            description=description,
            body=body,
            tags=tags or [],
            source="manual",
        )
        _print_memory_header("Memory created", project_path, store)
        _print_memory_item(item, include_body=False)
        return item

    if operation == "list":
        entries = store.list_memories()
        _print_memory_header("Memory list", project_path, store)
        _safe_print(f"Memories: {len(entries)}")
        for entry in entries:
            _print_memory_entry(entry)
        return entries

    if operation == "search":
        items = store.search_memories(
            query,
            memory_type=memory_type,
            limit=limit,
            include_body=True,
        )
        _print_memory_header("Memory search", project_path, store)
        _safe_print(f"Matches: {len(items)}")
        for item in items:
            _print_memory_item(item, include_body=False)
        return items

    if operation == "load":
        if not name:
            raise ValueError("Memory operation 'load' requires name.")
        item = store.load_memory(name)
        _print_memory_header("Memory loaded", project_path, store)
        _print_memory_item(item, include_body=True)
        return item

    if operation == "rebuild":
        entries = store.rebuild_index()
        _print_memory_header("Memory index rebuilt", project_path, store)
        _safe_print(f"Memories: {len(entries)}")
        for entry in entries:
            _print_memory_entry(entry)
        return entries

    raise ValueError(f"Unsupported memory operation: {operation}")


def task_project(
    project_path: Path,
    operation: str,
    task_id: str | None = None,
    *,
    title: str | None = None,
    description: str = "",
    blocked_by: list[str] | None = None,
    owner: str | None = None,
) -> list[TaskNode] | TaskNode:
    """Manage project-local Task DAG state under .pclens/tasks."""
    store = TaskDAGStore(project_path)

    if operation == "create":
        task = store.create_task(
            task_id=task_id,
            title=title or "",
            description=description,
            blocked_by=blocked_by or [],
            owner=owner,
        )
        _print_task_header("Task created", project_path, store)
        _print_task_node(task, store)
        return task

    if operation == "list":
        tasks = store.list_tasks()
        _print_task_header("Task list", project_path, store)
        _safe_print(f"Tasks: {len(tasks)}")
        for task in tasks:
            _print_task_node(task, store)
        return tasks

    if operation == "get":
        _require_task_id(operation, task_id)
        task = store.get_task(task_id or "")
        _print_task_header("Task loaded", project_path, store)
        _print_task_node(task, store)
        return task

    if operation == "claim":
        _require_task_id(operation, task_id)
        task = store.claim_task(task_id or "", owner=owner)
        _print_task_header("Task claimed", project_path, store)
        _print_task_node(task, store)
        return task

    if operation == "complete":
        _require_task_id(operation, task_id)
        task, ready_tasks = store.complete_task(task_id or "")
        _print_task_header("Task completed", project_path, store)
        _print_task_node(task, store)
        _safe_print(f"Ready tasks: {len(ready_tasks)}")
        for ready_task in ready_tasks:
            _print_task_node(ready_task, store)
        return task

    raise ValueError(f"Unsupported task operation: {operation}")


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


def _print_task_header(label: str, project_path: Path, store: TaskDAGStore) -> None:
    print(f"PyCode {label}.")
    print(f"Project path: {project_path}")
    print(f"Task storage: {store.tasks_dir}")


def _print_task_node(task: TaskNode, store: TaskDAGStore) -> None:
    can_start = store.can_start(task)
    blocked_by = ", ".join(task.blocked_by) if task.blocked_by else "N/A"
    blocked = ", ".join(can_start.blocked_by) if can_start.blocked_by else "N/A"
    missing = (
        ", ".join(can_start.missing_dependencies)
        if can_start.missing_dependencies
        else "N/A"
    )
    _safe_print(
        f"- {task.id}: {task.status} - {task.title} "
        f"(owner={task.owner or 'N/A'}, blocked_by={blocked_by}, "
        f"can_start={can_start.can_start}, active_blocks={blocked}, "
        f"missing={missing})"
    )


def _print_memory_header(label: str, project_path: Path, store: MemoryStore) -> None:
    print(f"PyCode {label}.")
    print(f"Project path: {project_path}")
    print(f"Memory storage: {store.memory_dir}")


def _print_memory_entry(entry: MemoryIndexEntry) -> None:
    tags = f", tags={','.join(entry.tags)}" if entry.tags else ""
    _safe_print(
        f"- {entry.name}: {entry.type} - {entry.description} "
        f"(path={entry.path}{tags})"
    )


def _print_memory_item(item: MemoryItem, *, include_body: bool) -> None:
    tags = f", tags={','.join(item.tags)}" if item.tags else ""
    _safe_print(
        f"- {item.name}: {item.type} - {item.description} "
        f"(path={item.path}, source={item.source}{tags})"
    )
    if include_body:
        _safe_print(item.body or "N/A")


def _require_task_id(operation: str, task_id: str | None) -> None:
    if not task_id:
        raise ValueError(f"Task operation '{operation}' requires task_id.")


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
    model: str | None,
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


def _print_agent_result(result: AgentResult, *, show_context: bool = False) -> None:
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

    _print_todo_summary(result)
    _print_memory_summary(result)
    _print_trace_summary(result)
    if show_context:
        _print_context_summary(result)

    print("Evidence:")
    evidence = _agent_evidence(result)
    if not evidence:
        print("- N/A")
    else:
        for item in evidence:
            _safe_print(f"- {item}")

    print("Answer:")
    if result.answer:
        _safe_print(result.answer)
    elif result.stop_reason == "plan_only":
        _safe_print("(plan only, no LLM summary generated.)")
    else:
        _safe_print("(no LLM summary generated.)")


def _print_todo_summary(result: AgentResult) -> None:
    if not result.todos:
        return

    counts = {
        "pending": 0,
        "in_progress": 0,
        "completed": 0,
        "failed": 0,
    }
    current = "N/A"
    for item in result.todos:
        counts[item.status] = counts.get(item.status, 0) + 1
        if item.status == "in_progress":
            current = item.id

    print("Todos:")
    _safe_print(
        "- progress: "
        f"total={len(result.todos)}, completed={counts['completed']}, "
        f"failed={counts['failed']}, pending={counts['pending']}, "
        f"in_progress={counts['in_progress']}, current={current}"
    )
    for item in result.todos:
        detail = (
            f"- {item.id}: {item.status} - {item.tool} - "
            f"{item.title or item.reason or 'N/A'}"
        )
        if item.error:
            detail += f" Error: {item.error}"
        _safe_print(detail)


def _print_memory_summary(result: AgentResult) -> None:
    if result.memory is None:
        return

    summary = result.memory.summary()
    print("Memories:")
    _safe_print(
        "- counts: "
        f"index={summary['index_entries']}, "
        f"relevant={summary['relevant_memories']}, "
        f"extracted={summary['extracted_memories']}"
    )
    if result.memory.selection_error:
        _safe_print(f"- selection_error: {result.memory.selection_error}")
    if result.memory.extraction_error:
        _safe_print(f"- extraction_error: {result.memory.extraction_error}")
    for item in result.memory.relevant_memories:
        _safe_print(f"- relevant: {item.name} ({item.type}) - {item.description}")
    for item in result.memory.extracted_memories:
        _safe_print(f"- extracted: {item.name} ({item.type}) - {item.description}")


def _print_trace_summary(result: AgentResult) -> None:
    trace = result.trace
    if trace is None:
        return

    summary = trace.summary()
    print("Trace:")
    _safe_print(f"- run_id: {trace.run_id}")
    _safe_print(f"- duration_ms: {trace.duration_ms if trace.duration_ms is not None else 'N/A'}")
    _safe_print(
        "- counts: "
        f"events={summary['events']}, tools={summary['tools']}, "
        f"ok={summary['ok']}, failed={summary['failed']}, denied={summary['denied']}"
    )
    for tool_trace in trace.tools:
        detail = (
            f"- turn {tool_trace.turn_index}: {tool_trace.tool} -> "
            f"{tool_trace.status}"
        )
        if tool_trace.duration_ms is not None:
            detail += f" ({tool_trace.duration_ms} ms)"
        if tool_trace.summary:
            detail += f" - {tool_trace.summary}"
        if tool_trace.denied_by:
            detail += f" [denied_by={tool_trace.denied_by}]"
        if tool_trace.error:
            detail += f" Error: {tool_trace.error}"
        _safe_print(detail)


def _print_context_summary(result: AgentResult) -> None:
    context = result.context
    if context is None:
        return

    print("Context:")
    _safe_print(f"- sections: {len(context.sections)}")
    for section in context.sections:
        _safe_print(
            f"- {section.name}: placement={section.placement}, "
            f"source={section.source}, chars={len(section.content)}"
        )
    if context.warnings:
        print("Context warnings:")
        for warning in context.warnings:
            _safe_print(f"- {warning}")


def _agent_evidence(result: AgentResult) -> list[str]:
    evidence: list[str] = []
    if result.memory is not None:
        for item in result.memory.relevant_memories:
            if item.path:
                evidence.append(f".pclens/memory/{item.path}")
        for item in result.memory.extracted_memories:
            if item.path:
                evidence.append(f".pclens/memory/{item.path}")
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
    return dedupe_preserve_order(evidence)


def _safe_print(text: str) -> None:
    global _CONFIGURED_STDOUT_ID
    stdout_id = id(sys.stdout)
    if _CONFIGURED_STDOUT_ID != stdout_id:
        reconfigure = getattr(sys.stdout, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(errors="replace")
                _CONFIGURED_STDOUT_ID = stdout_id
                print(text)
                return
            except (AttributeError, OSError, TypeError, ValueError):
                _CONFIGURED_STDOUT_ID = stdout_id
    encoding = sys.stdout.encoding or "utf-8"
    safe_text = text.encode(encoding, errors="replace").decode(encoding)
    print(safe_text)


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
    agent_parser.add_argument(
        "--no-memory",
        action="store_true",
        help="Disable loading project memories for this Agent run.",
    )
    agent_parser.add_argument(
        "--no-memory-extract",
        action="store_true",
        help="Disable automatic memory extraction after this Agent run.",
    )
    agent_parser.add_argument(
        "--show-context",
        action="store_true",
        help="Show Agent context section metadata without printing the full prompt.",
    )
    _add_model_argument(agent_parser)

    memory_parser = subparsers.add_parser(
        "memory",
        help="Manage project-local persistent memories.",
    )
    memory_parser.add_argument(
        "project_path",
        type=Path,
        help="Python project directory containing .pclens/memory.",
    )
    memory_parser.add_argument(
        "operation",
        choices=["add", "list", "search", "load", "rebuild"],
        help="Memory operation to run.",
    )
    memory_parser.add_argument(
        "name",
        nargs="?",
        help="Memory name for load.",
    )
    memory_parser.add_argument(
        "--name",
        dest="explicit_name",
        default=None,
        help="Memory name for add.",
    )
    memory_parser.add_argument(
        "--type",
        dest="memory_type",
        default=None,
        choices=["user", "feedback", "project", "reference"],
        help="Memory type for add/search.",
    )
    memory_parser.add_argument(
        "--description",
        default="",
        help="Short memory description for add.",
    )
    memory_parser.add_argument(
        "--content",
        dest="body",
        default=None,
        help="Markdown memory body for add.",
    )
    memory_parser.add_argument(
        "--tag",
        dest="tags",
        action="append",
        default=[],
        help="Memory tag. Can be provided multiple times.",
    )
    memory_parser.add_argument(
        "--query",
        default="",
        help="Search query for memory search.",
    )
    memory_parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Maximum memories to return for search.",
    )

    task_parser = subparsers.add_parser(
        "task",
        help="Manage project-local Task DAG files.",
    )
    task_parser.add_argument(
        "project_path",
        type=Path,
        help="Python project directory containing .pclens/tasks.",
    )
    task_parser.add_argument(
        "operation",
        choices=["create", "list", "get", "claim", "complete"],
        help="Task DAG operation to run.",
    )
    task_parser.add_argument(
        "task_id",
        nargs="?",
        help="Task id for get, claim, or complete.",
    )
    task_parser.add_argument(
        "--id",
        dest="explicit_task_id",
        default=None,
        help="Explicit task id to use when creating a task.",
    )
    task_parser.add_argument(
        "--title",
        default=None,
        help="Task title for create.",
    )
    task_parser.add_argument(
        "--description",
        default="",
        help="Task description for create.",
    )
    task_parser.add_argument(
        "--blocked-by",
        action="append",
        default=[],
        help="Dependency task id. Can be provided multiple times.",
    )
    task_parser.add_argument(
        "--owner",
        default=None,
        help="Task owner for create or claim.",
    )

    return parser


def _add_model_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--model",
        default=None,
        help="OpenAI model to use. Defaults to OPENAI_MODEL from the shell or .env.",
    )


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    try:
        _dispatch_command(args)
    except (FileNotFoundError, PermissionError, ValueError, RuntimeError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


def _dispatch_command(args: argparse.Namespace) -> None:
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
            enable_memory=not args.no_memory,
            enable_memory_extraction=not args.no_memory_extract,
            show_context=args.show_context,
        )
    elif args.command == "memory":
        memory_project(
            args.project_path,
            args.operation,
            name=args.explicit_name if args.operation == "add" else args.name,
            memory_type=args.memory_type,
            description=args.description,
            body=args.body,
            tags=args.tags,
            query=args.query,
            limit=args.limit,
        )
    elif args.command == "task":
        task_project(
            args.project_path,
            args.operation,
            task_id=args.explicit_task_id if args.operation == "create" else args.task_id,
            title=args.title,
            description=args.description,
            blocked_by=args.blocked_by,
            owner=args.owner,
        )


if __name__ == "__main__":
    main()
