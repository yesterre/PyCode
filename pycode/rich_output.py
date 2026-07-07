from __future__ import annotations

from pathlib import Path
from typing import Any, TextIO

from pycode.agent import AgentResult
from pycode.models import CodeGraph, GraphEdge, GraphNode, ProjectIndex
from pycode.utils import dedupe_preserve_order

try:  # pragma: no cover - exercised when the optional dependency is absent.
    from rich import box
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.tree import Tree

    RICH_AVAILABLE = True
except ImportError:  # pragma: no cover - fallback path is environment-specific.
    box = None
    Console = None
    Panel = None
    Table = None
    Text = None
    Tree = None
    RICH_AVAILABLE = False


def rich_available() -> bool:
    return RICH_AVAILABLE


def make_console(file: TextIO | None = None) -> Any:
    if not RICH_AVAILABLE:
        return None
    return Console(file=file, soft_wrap=True)


def print_index_summary_rich(
    index: ProjectIndex,
    output_path: Path,
    *,
    console: Any | None = None,
) -> bool:
    if not RICH_AVAILABLE:
        return False
    console = console or make_console()
    table = Table(title="PyCode Index Completed", box=box.SIMPLE_HEAVY)
    table.add_column("Metric", style="cyan", no_wrap=True)
    table.add_column("Value", style="green")
    table.add_row("Project path", str(index.project_path))
    table.add_row("Python files", str(len(index.files)))
    table.add_row("Imports", str(sum(len(file.imports) for file in index.files)))
    table.add_row("Classes", str(sum(len(file.classes) for file in index.files)))
    table.add_row("Functions", str(sum(len(file.functions) for file in index.files)))
    table.add_row("Index file", str(output_path))
    console.print(table)
    console.print(build_project_tree(index))
    return True


def print_graph_summary_rich(
    graph: CodeGraph,
    output_path: Path,
    *,
    console: Any | None = None,
) -> bool:
    if not RICH_AVAILABLE:
        return False
    console = console or make_console()
    node_counts = _count_by_type(graph.nodes)
    edge_counts = _count_by_type(graph.edges)
    table = Table(title="PyCode Graph Completed", box=box.SIMPLE_HEAVY)
    table.add_column("Metric", style="cyan", no_wrap=True)
    table.add_column("Value", style="green")
    table.add_row("Project path", str(graph.project_path))
    table.add_row("Nodes", str(len(graph.nodes)))
    table.add_row("Edges", str(len(graph.edges)))
    table.add_row("File nodes", str(node_counts.get("file", 0)))
    table.add_row("Class nodes", str(node_counts.get("class", 0)))
    table.add_row("Function nodes", str(node_counts.get("function", 0)))
    table.add_row("Method nodes", str(node_counts.get("method", 0)))
    table.add_row("Import edges", str(edge_counts.get("imports", 0)))
    table.add_row("Call edges", str(edge_counts.get("calls", 0)))
    table.add_row("Graph file", str(output_path))
    console.print(table)
    return True


def print_query_result_rich(
    query_type: str,
    result: list[GraphEdge] | list[GraphNode],
    graph_path: Path,
    *,
    console: Any | None = None,
) -> bool:
    if not RICH_AVAILABLE:
        return False
    console = console or make_console()
    table = Table(title="PyCode Query Completed", box=box.SIMPLE_HEAVY)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value", style="green")
    table.add_row("Graph file", str(graph_path))
    table.add_row("Query", query_type)
    table.add_row("Results", str(len(result)))
    console.print(table)

    if not result:
        return True

    result_table = Table(title="Query Results", box=box.SIMPLE)
    if isinstance(result[0], GraphEdge):
        result_table.add_column("Source", style="magenta")
        result_table.add_column("Type", style="cyan")
        result_table.add_column("Target", style="green")
        for edge in result:
            result_table.add_row(edge.source, edge.type, edge.target)
    else:
        result_table.add_column("Node", style="magenta")
        result_table.add_column("Type", style="cyan")
        result_table.add_column("Path", style="green")
        for node in result:
            result_table.add_row(node.id, node.type, node.path or "N/A")
    console.print(result_table)
    return True


def print_llm_answer_rich(
    *,
    intent: str,
    answer: str,
    evidence: list[str],
    console: Any | None = None,
) -> bool:
    if not RICH_AVAILABLE:
        return False
    console = console or make_console()
    console.print(Panel(answer or "N/A", title=f"PyCode Answer: {intent}", border_style="green"))
    evidence_table = Table(title="Evidence", box=box.SIMPLE)
    evidence_table.add_column("Location", style="cyan")
    for item in evidence or ["N/A"]:
        evidence_table.add_row(format_code_location(item))
    console.print(evidence_table)
    return True


def print_agent_result_rich(
    result: AgentResult,
    *,
    show_context: bool = False,
    console: Any | None = None,
) -> bool:
    if not RICH_AVAILABLE:
        return False
    console = console or make_console()
    header = Table(title="PyCode Agent Completed", box=box.SIMPLE_HEAVY)
    header.add_column("Field", style="cyan", no_wrap=True)
    header.add_column("Value", style="green")
    header.add_row("Task", result.task.description)
    header.add_row("Task type", result.task.task_type)
    header.add_row("Planner", result.planner_source)
    if result.planner_error:
        header.add_row("Planner fallback reason", result.planner_error)
    header.add_row("Project path", str(result.task.project_path))
    header.add_row("Tests allowed", str(result.task.allow_tests))
    header.add_row("Stop reason", str(result.stop_reason))
    header.add_row("Runtime turns", str(len(result.turns)))
    header.add_row("Steps", str(len(result.steps)))
    console.print(header)

    _print_steps(console, result)
    _print_runtime(console, result)
    _print_todos(console, result)
    _print_memory(console, result)
    _print_trace(console, result)
    if show_context:
        _print_context(console, result)
    _print_evidence(console, _agent_evidence(result))
    console.print(
        Panel(
            result.answer
            or (
                "(plan only, no LLM summary generated.)"
                if result.stop_reason == "plan_only"
                else "(no LLM summary generated.)"
            ),
            title="Answer",
            border_style="green",
        )
    )
    return True


def build_project_tree(index: ProjectIndex) -> Any:
    if not RICH_AVAILABLE:
        return None
    root_name = Path(index.project_path).name or str(index.project_path)
    root = Tree(f"[bold cyan]{root_name}[/bold cyan]")
    branches: dict[tuple[str, ...], Any] = {(): root}
    for file_info in sorted(index.files, key=lambda item: item.path):
        parts = Path(file_info.path).parts
        parent_key: tuple[str, ...] = ()
        for directory in parts[:-1]:
            key = parent_key + (directory,)
            if key not in branches:
                branches[key] = branches[parent_key].add(f"[bold]{directory}[/bold]")
            parent_key = key
        label = parts[-1] if parts else file_info.path
        details = (
            f" [dim]imports={len(file_info.imports)} "
            f"classes={len(file_info.classes)} functions={len(file_info.functions)}[/dim]"
        )
        branches[parent_key].add(f"{label}{details}")
    return root


def format_code_location(value: str) -> str:
    text = str(value)
    if " --" in text and "-->" in text:
        return text
    if text.startswith(("file:", "class:", "func:", "method:")):
        return text
    if ":" in text:
        path, _, suffix = text.rpartition(":")
        if path and suffix.isdigit():
            return f"{path}:{suffix}"
    return text


def _print_steps(console: Any, result: AgentResult) -> None:
    table = Table(title="Plan / Tool Steps", box=box.SIMPLE)
    table.add_column("#", justify="right", style="cyan", no_wrap=True)
    table.add_column("Tool", style="magenta")
    table.add_column("Status", style="green")
    table.add_column("Summary / Reason")
    for index, step in enumerate(result.steps, start=1):
        if index <= len(result.tool_results):
            tool_result = result.tool_results[index - 1]
            status = "ok" if tool_result.ok else "failed"
            summary = tool_result.summary
            if tool_result.error:
                summary += f" Error: {tool_result.error}"
        else:
            status = "planned"
            summary = step.reason or "N/A"
        table.add_row(str(index), step.tool, status, summary)
    console.print(table)


def _print_runtime(console: Any, result: AgentResult) -> None:
    if not result.turns:
        return
    table = Table(title="Runtime Turns", box=box.SIMPLE)
    table.add_column("Turn", justify="right", style="cyan")
    table.add_column("Tool", style="magenta")
    table.add_column("Status", style="green")
    table.add_column("Summary")
    for turn in result.turns:
        status = "ok" if turn.tool_result.ok else "failed"
        table.add_row(
            str(turn.index),
            turn.tool_call.name,
            status,
            turn.tool_result.summary,
        )
    console.print(table)


def _print_todos(console: Any, result: AgentResult) -> None:
    if not result.todos:
        return
    counts = {"pending": 0, "in_progress": 0, "completed": 0, "failed": 0}
    for item in result.todos:
        counts[item.status] = counts.get(item.status, 0) + 1
    table = Table(
        title=(
            "Todos "
            f"(total={len(result.todos)}, completed={counts['completed']}, "
            f"failed={counts['failed']}, pending={counts['pending']})"
        ),
        box=box.SIMPLE,
    )
    table.add_column("ID", style="cyan")
    table.add_column("Status", style="green")
    table.add_column("Tool", style="magenta")
    table.add_column("Title")
    for item in result.todos:
        title = item.title or item.reason or "N/A"
        if item.error:
            title += f" Error: {item.error}"
        table.add_row(item.id, item.status, item.tool, title)
    console.print(table)


def _print_memory(console: Any, result: AgentResult) -> None:
    if result.memory is None:
        return
    summary = result.memory.summary()
    table = Table(title="Memories", box=box.SIMPLE)
    table.add_column("Kind", style="cyan")
    table.add_column("Count / Detail", style="green")
    table.add_row("index", str(summary["index_entries"]))
    table.add_row("relevant", str(summary["relevant_memories"]))
    table.add_row("extracted", str(summary["extracted_memories"]))
    if result.memory.selection_error:
        table.add_row("selection_error", result.memory.selection_error)
    if result.memory.extraction_error:
        table.add_row("extraction_error", result.memory.extraction_error)
    for item in result.memory.relevant_memories:
        table.add_row("relevant", f"{item.name} ({item.type}) - {item.description}")
    for item in result.memory.extracted_memories:
        table.add_row("extracted", f"{item.name} ({item.type}) - {item.description}")
    console.print(table)


def _print_trace(console: Any, result: AgentResult) -> None:
    trace = result.trace
    if trace is None:
        return
    summary = trace.summary()
    table = Table(
        title=(
            f"Trace {trace.run_id} "
            f"(events={summary['events']}, tools={summary['tools']}, "
            f"ok={summary['ok']}, failed={summary['failed']}, denied={summary['denied']})"
        ),
        box=box.SIMPLE,
    )
    table.add_column("Turn", justify="right", style="cyan")
    table.add_column("Tool", style="magenta")
    table.add_column("Status", style="green")
    table.add_column("Duration")
    table.add_column("Summary")
    for tool_trace in trace.tools:
        duration = (
            f"{tool_trace.duration_ms} ms"
            if tool_trace.duration_ms is not None
            else "N/A"
        )
        summary_text = tool_trace.summary
        if tool_trace.denied_by:
            summary_text += f" [denied_by={tool_trace.denied_by}]"
        if tool_trace.error:
            summary_text += f" Error: {tool_trace.error}"
        table.add_row(
            str(tool_trace.turn_index),
            tool_trace.tool,
            tool_trace.status,
            duration,
            summary_text,
        )
    console.print(table)


def _print_context(console: Any, result: AgentResult) -> None:
    context = result.context
    if context is None:
        return
    table = Table(title=f"Context Sections ({len(context.sections)})", box=box.SIMPLE)
    table.add_column("Name", style="cyan")
    table.add_column("Placement", style="green")
    table.add_column("Source", style="magenta")
    table.add_column("Chars", justify="right")
    for section in context.sections:
        table.add_row(
            section.name,
            section.placement,
            section.source,
            str(len(section.content)),
        )
    console.print(table)
    if context.warnings:
        console.print(Panel("\n".join(context.warnings), title="Context Warnings"))


def _print_evidence(console: Any, evidence: list[str]) -> None:
    table = Table(title="Evidence", box=box.SIMPLE)
    table.add_column("Location", style="cyan")
    for item in evidence or ["N/A"]:
        table.add_row(format_code_location(item))
    console.print(table)


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


def _count_by_type(items: list[GraphNode] | list[GraphEdge]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        counts[item.type] = counts.get(item.type, 0) + 1
    return counts
