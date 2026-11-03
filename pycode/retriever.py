"""One lexical/Graph retrieval core for Fast Path and Agent tools.

The retrieve_* functions are compatibility adapters. Callers never reselect or
crop code from the selected structured EvidenceItems.
"""
import re
from pathlib import Path
from time import perf_counter

from pycode.models import CodeGraph, ProjectIndex
from pycode.query import find_entry_candidates, get_file_imported_by, get_file_imports
from pycode.retrieval_evidence import (
    EvidenceItem, Query, RetrievalContext, RetrievalRequest, RetrievalResult,
)
from pycode.utils import dedupe_preserve_order, normalize_path
from pycode.semantic_search import VectorUnavailable

ContextItem = EvidenceItem


def retrieve(request: RetrievalRequest, context: RetrievalContext) -> RetrievalResult:
    started = perf_counter()
    query = request.query
    intent = query.intent or _detect_intent(query.text)
    if intent not in {"general", "explain", "impact", "onboard", "entry", "dependency"}:
        raise ValueError("Unsupported retrieval intent.")
    graph = context.graph
    paths = {file.path for file in context.index.files}
    target = query.target or _best_file_match(query.text, context.index)
    specs = []
    diagnostics = list(context.diagnostics)
    if intent in {"impact", "explain"} and query.target and query.target not in paths:
        diagnostics.append("target_not_indexed")
    elif intent in {"impact", "dependency", "explain"} and target:
        specs.append((target, "exact_path", "explicit_target", 100.0, None, "definition"))
        if intent != "explain" and graph is not None:
            for edge in get_file_imported_by(graph, target) + get_file_imports(graph, target):
                node = edge.source if edge.target == f"file:{target}" else edge.target
                if node.startswith("file:") and node[5:] in paths:
                    specs.append((node[5:], "graph", f"stored_relation:{edge.source} --{edge.type}--> {edge.target}",
                                  1.0, None, "dependency"))
    elif intent in {"entry", "onboard"}:
        entries = find_entry_candidates(graph) if graph is not None else []
        selected = dedupe_preserve_order(node.path for node in entries if node.path in paths)
        if not selected and intent == "onboard":
            selected = sorted(paths)[:3]
            diagnostics.append("entry_unknown_reading_order_only")
        for path in selected:
            specs.append((path, "entry", "entry_candidate_not_proven_runtime_entry", 1.0, None, "definition"))
        if intent == "onboard" and graph is not None:
            for path in selected:
                for edge in get_file_imports(graph, path):
                    if edge.target.startswith("file:") and edge.target[5:] in paths:
                        specs.append((edge.target[5:], "graph", "entry_direct_import", 1.0, None, "dependency"))
    else:
        specs = _lexical_candidates(query.text, context)
        # Explicit targets and existing intent strategies keep their semantics.
        # This batch provides a vector route, not fusion of candidate channels.
        if context.retrieval_mode == "vector" and not any(
            spec[1] in {"exact_path", "exact_symbol"} for spec in specs
        ):
            try:
                if context.binding is None or context.chunks is None:
                    raise VectorUnavailable("vector_binding_mismatch")
                if context.vector_query is None:
                    raise VectorUnavailable()
                result = context.vector_query(query.text, min(request.limit, context.vector_top_k))
                if result.binding != context.binding:
                    raise VectorUnavailable("vector_binding_mismatch")
                # The provider bridge returns only spans from the trusted Chunk
                # set. Reject forged/mismatched results even in injected callers.
                known = {(c.path, c.span) for c in context.chunks.chunks}
                if any((hit.path, hit.span) not in known for hit in result.hits):
                    raise VectorUnavailable("vector_binding_mismatch")
                specs = [(hit.path, "vector", "cosine_max_over_definition_segments", hit.score,
                          hit.span if hit.span.symbol_kind != "module" else None,
                          "definition") for hit in result.hits]
            except VectorUnavailable as exc:
                diagnostics.append(exc.code)
    items, seen = [], set()
    for path, source, reason, score, span, role in specs:
        key = (path, span.symbol_key if span else None)
        if key in seen:
            continue
        seen.add(key)
        if len(items) >= request.limit:
            diagnostics.append("candidate_limit_reached")
            break
        items.append(context.item(path, reason=reason, source=source, score=score, span=span, role=role))
    diagnostics.extend(code for item in items for code in item.diagnostics)
    diagnostics = dedupe_preserve_order(diagnostics)
    status = "empty" if not items else "unavailable" if all(i.coverage == "unavailable" for i in items) else "degraded" if diagnostics else "ok"
    if not items and any(code.startswith("vector_") for code in diagnostics):
        status = "degraded"  # Failed channel + empty fallback is not a clean miss.
    return RetrievalResult(query.text, intent, items, context.binding, status,
                           tuple(diagnostics), round((perf_counter() - started) * 1000, 3))


def _lexical_candidates(question: str, context: RetrievalContext) -> list:
    tokens = _tokens(question)
    sources = dict(context.sources)
    exact_paths = [file.path for file in context.index.files
                   if _mentions(normalize_path(question), file.path)]
    symbols = {}
    for chunk in context.chunks.chunks if context.chunks else []:
        if chunk.span.symbol_kind != "module":
            symbols[(chunk.path, chunk.span.symbol_key)] = chunk.span
    exact_symbols = [(path, span) for (path, _), span in symbols.items()
                     if _mentions(question, span.qualified_name) or _mentions(question, span.symbol_name)]
    if exact_paths:
        exact_symbols = [(path, span) for path, span in exact_symbols if path in exact_paths]
    if exact_symbols:
        return [(path, "exact_symbol", "exact_symbol_match", 100.0, span, "definition")
                for path, span in sorted(exact_symbols, key=lambda v: (v[0], v[1].symbol_start_line))]
    if exact_paths:
        return [(path, "exact_path", "exact_path_match", 100.0, None, "definition") for path in sorted(exact_paths)]
    scored = []
    for file in context.index.files:
        metadata = " ".join([file.path, *file.imports, *file.functions,
                             *(c.name for c in file.classes), *(m for c in file.classes for m in c.methods)]).lower()
        text = sources.get(file.path, "").lower()
        file_score = sum(token in metadata for token in tokens)
        file_symbols = [(span, sum(token in "\n".join(
            text.split("\n")[span.symbol_start_line - 1:span.symbol_end_line]) for token in tokens))
            for (path, _), span in symbols.items() if path == file.path]
        matches = [(span, score) for span, score in file_symbols if score]
        if matches:
            for span, score in matches:
                scored.append((file_score + score, file.path, span))
        elif file_score or any(token in text for token in tokens):
            scored.append((file_score + sum(token in text for token in tokens), file.path, None))
    return [(path, "keyword", "lexical_metadata_or_code_match", float(score), span, "definition")
            for score, path, span in sorted(scored, key=lambda v: (-v[0], v[1], v[2].symbol_start_line if v[2] else 0))]


def _mentions(text: str, value: str) -> bool:
    return bool(value and re.search(r"(?<![A-Za-z0-9_./])" + re.escape(value) + r"(?![A-Za-z0-9_/]|\.[A-Za-z_])", text, re.IGNORECASE))


def _detect_intent(question: str) -> str:
    text = question.lower()
    if any(word in text for word in ["入口", "启动"]) or re.search(r"\b(entry|main)\b", text):
        return "entry"
    if any(word in text for word in ["影响", "impact", "改动", "修改"]):
        return "impact"
    if any(word in text for word in ["依赖", "import", "调用", "call"]):
        return "dependency"
    return "general"


def _best_file_match(question: str, index: ProjectIndex) -> str | None:
    question = normalize_path(question)
    matches = [file.path for file in index.files if _mentions(question, file.path)]
    if len(matches) == 1:
        return matches[0]
    names = [file.path for file in index.files if _mentions(question, file.path.rsplit("/", 1)[-1])]
    return names[0] if len(names) == 1 else None


def _tokens(text: str) -> list[str]:
    return dedupe_preserve_order(token for token in re.findall(r"[\w/]+", normalize_path(text).lower()) if len(token) >= 2)


def _context(root, index, graph, context):
    if context is not None:
        if Path(root).resolve() != context.root:
            raise ValueError("Retrieval context belongs to another workspace.")
        return context
    return RetrievalContext.create(Path(root), index, graph)


def retrieve_for_question(question: str, project_path: Path, index: ProjectIndex,
                          graph: CodeGraph, *, context: RetrievalContext | None = None) -> RetrievalResult:
    return retrieve(RetrievalRequest(Query(question)), _context(project_path, index, graph, context))


def retrieve_explain(file_path: str, project_path: Path, index: ProjectIndex, graph: CodeGraph,
                     question: str | None = None, *, context: RetrievalContext | None = None) -> RetrievalResult:
    return retrieve(RetrievalRequest(Query(question or f"解释文件 {file_path}", "explain", normalize_path(file_path))),
                    _context(project_path, index, graph, context))


def retrieve_impact(file_path: str, project_path: Path, index: ProjectIndex, graph: CodeGraph,
                    question: str | None = None, *, context: RetrievalContext | None = None) -> RetrievalResult:
    return retrieve(RetrievalRequest(Query(question or f"分析修改 {file_path} 的影响范围", "impact", normalize_path(file_path)), 8),
                    _context(project_path, index, graph, context))


def retrieve_onboard(project_path: Path, index: ProjectIndex, graph: CodeGraph,
                     *, context: RetrievalContext | None = None) -> RetrievalResult:
    return retrieve(RetrievalRequest(Query("生成项目新手阅读顺序", "onboard")), _context(project_path, index, graph, context))
