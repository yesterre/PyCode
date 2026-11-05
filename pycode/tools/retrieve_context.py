from pathlib import Path

from pycode.constants import DEFAULT_ARTIFACT_DIR, DEFAULT_GRAPH_FILE, DEFAULT_INDEX_FILE
from pycode.retriever import retrieve
from pycode.retrieval_evidence import Query, RetrievalContext, RetrievalRequest
from pycode.storage import load_graph, load_index
from pycode.tools.base import ToolContext, failure, success
from pycode.evidence_pack import tool_items


def retrieve_context(
    context: ToolContext,
    question: str,
    *,
    target: str | None = None,
    intent: str | None = None,
    graph_path: str | Path | None = None,
    index_path: str | Path | None = None,
    max_snippet_chars: int | None = None,
):
    """Render the common evidence selection without cropping or selecting again.

    max_snippet_chars is accepted for old callers but no longer crops evidence.
    Trusted Snapshot inputs cannot be replaced by model-supplied artifact paths.
    """
    trusted = context.retrieval_context
    if trusted is not None:
        if index_path is not None or graph_path is not None:
            return failure("retrieve_context", "Artifact path denied.", "trusted_artifacts_cannot_be_overridden")
        if trusted.root != context.project_root:
            return failure("retrieve_context", "Artifact path denied.", "trusted_workspace_mismatch")
        seed = context.initial_evidence
        if seed is not None:
            try:
                seed.validate(trusted)
                if seed.matches(question, target, intent):
                    data = seed.projection()
                    data['compatibility_notes'] = ['max_snippet_chars_ignored_to_preserve_evidence'] if max_snippet_chars is not None else []
                    data['initial_evidence_reused'] = True
                    return success('retrieve_context', f'Selected {len(seed.result.items)} context items (initial Pack reused).', **data)
            except ValueError:
                return failure('retrieve_context', 'Initial evidence denied.', 'initial_evidence_mismatch')
        return _retrieve(trusted, question, target, intent, max_snippet_chars)
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
    except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError) as exc:
        return failure("retrieve_context", "PyCode artifacts cannot be loaded.", str(exc))
    try:
        trusted = RetrievalContext.create(context.project_root, index, graph)
    except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError) as exc:
        return failure("retrieve_context", "PyCode artifacts cannot be loaded.", str(exc))
    return _retrieve(trusted, question, target, intent, max_snippet_chars)


def _retrieve(trusted, question, target, intent, max_snippet_chars):
    try:
        normalized_intent = (intent or "").lower() or None
        retrieval = retrieve(RetrievalRequest(Query(
            question, normalized_intent, target.replace("\\", "/") if target else None,
        ), 8 if normalized_intent == "impact" else 6), trusted)
    except ValueError:
        return failure("retrieve_context", "Invalid retrieval request.", "invalid_retrieval_request")
    return success(
        "retrieve_context",
        f"Selected {len(retrieval.items)} context items.",
        question=question,
        intent=retrieval.intent,
        evidence=retrieval.evidence,
        items=tool_items(retrieval.items),
        retrieval=retrieval.summary(),
        compatibility_notes=["max_snippet_chars_ignored_to_preserve_evidence"] if max_snippet_chars is not None else [],
    )
