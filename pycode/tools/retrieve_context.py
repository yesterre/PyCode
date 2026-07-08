from pathlib import Path

from pycode.constants import DEFAULT_ARTIFACT_DIR, DEFAULT_GRAPH_FILE, DEFAULT_INDEX_FILE
from pycode.retriever import (
    RetrievalResult,
    retrieve_explain,
    retrieve_for_question,
    retrieve_impact,
    retrieve_onboard,
)
from pycode.storage import load_graph, load_index
from pycode.tools.base import ToolContext, failure, success, truncate_text


def retrieve_context(
    context: ToolContext,
    question: str,
    *,
    target: str | None = None,
    intent: str | None = None,
    graph_path: str | Path | None = None,
    index_path: str | Path | None = None,
    max_snippet_chars: int = 1200,
):
    """Reuse stage-3 retrieval to select code context for an Agent task."""
    try:
        resolved_index_path = context.resolve_in_project(
            index_path or f"{DEFAULT_ARTIFACT_DIR}/{DEFAULT_INDEX_FILE}"
        )
        resolved_graph_path = context.resolve_in_project(
            graph_path or f"{DEFAULT_ARTIFACT_DIR}/{DEFAULT_GRAPH_FILE}"
        )
    except PermissionError as exc:
        return failure("retrieve_context", "Artifact path denied.", str(exc))

    try:
        index = load_index(resolved_index_path)
        graph = load_graph(resolved_graph_path)
    except (FileNotFoundError, IsADirectoryError) as exc:
        return failure("retrieve_context", "PyCode artifacts cannot be loaded.", str(exc))

    normalized_intent = (intent or "").lower()
    if normalized_intent == "impact" and target:
        retrieval = retrieve_impact(target, context.project_root, index, graph, question)
    elif normalized_intent == "explain" and target:
        retrieval = retrieve_explain(target, context.project_root, index, graph, question)
    elif normalized_intent == "onboard":
        retrieval = retrieve_onboard(context.project_root, index, graph)
    elif normalized_intent in {"entry", "dependency"}:
        retrieval = retrieve_for_question(
            _intent_question(question, normalized_intent),
            context.project_root,
            index,
            graph,
        )
    else:
        retrieval = retrieve_for_question(question, context.project_root, index, graph)

    return success(
        "retrieve_context",
        f"Selected {len(retrieval.items)} context items.",
        question=question,
        intent=retrieval.intent,
        evidence=retrieval.evidence,
        items=[_item_data(item, max_snippet_chars) for item in retrieval.items],
    )


def _item_data(item, max_snippet_chars: int) -> dict[str, object]:
    snippet, truncated = truncate_text(item.snippet, max_snippet_chars)
    return {
        "title": item.title,
        "path": item.path,
        "node_ids": item.node_ids,
        "edges": item.edges,
        "reason": item.reason,
        "snippet": snippet,
        "snippet_truncated": truncated,
    }


def _intent_question(question: str, intent: str) -> str:
    if intent == "entry":
        return f"entry main \u5165\u53e3 {question}"
    if intent == "dependency":
        return f"dependency import call \u4f9d\u8d56 {question}"
    return question
