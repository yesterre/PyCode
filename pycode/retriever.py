from dataclasses import dataclass, field
from pathlib import Path

from pycode.models import CodeGraph, GraphEdge, GraphNode, ProjectIndex
from pycode.query import (
    find_entry_candidates,
    get_file_imported_by,
    get_file_imports,
)


MAX_SNIPPET_LINES = 80


@dataclass
class ContextItem:
    title: str
    path: str | None = None
    node_ids: list[str] = field(default_factory=list)
    edges: list[str] = field(default_factory=list)
    snippet: str = ""
    reason: str = ""


@dataclass
class RetrievalResult:
    question: str
    intent: str
    items: list[ContextItem] = field(default_factory=list)

    @property
    def evidence(self) -> list[str]:
        evidence_items: list[str] = []
        for item in self.items:
            if item.path:
                evidence_items.append(item.path)
            evidence_items.extend(item.node_ids)
            evidence_items.extend(item.edges)
        return _dedupe(evidence_items)


def retrieve_for_question(
    question: str,
    project_path: Path,
    index: ProjectIndex,
    graph: CodeGraph,
) -> RetrievalResult:
    """Select a small set of indexed code context for a natural-language question."""
    intent = _detect_intent(question)
    if intent == "entry":
        return _retrieve_entry(question, project_path, index, graph)
    if intent == "impact":
        target = _best_file_match(question, index)
        if target is not None:
            return retrieve_impact(target, project_path, index, graph, question)
    if intent == "dependency":
        return _retrieve_dependency(question, project_path, index, graph)

    return _retrieve_keyword(question, project_path, index, graph)


def retrieve_explain(
    file_path: str,
    project_path: Path,
    index: ProjectIndex,
    graph: CodeGraph,
    question: str | None = None,
) -> RetrievalResult:
    """Return context for explaining one file only."""
    normalized = _normalize_path(file_path)
    file_info = _file_by_path(index).get(normalized)
    item = ContextItem(
        title=f"Explain file {normalized}",
        path=normalized,
        node_ids=_node_ids_for_path(graph, normalized),
        edges=_edge_descriptions(_edges_for_path(graph, normalized)),
        snippet=_read_snippet(project_path, normalized),
        reason="用户指定的目标文件。",
    )
    if file_info is not None:
        item.reason = _file_summary(file_info)
    return RetrievalResult(
        question=question or f"解释文件 {normalized}",
        intent="explain",
        items=[item],
    )


def retrieve_onboard(
    project_path: Path,
    index: ProjectIndex,
    graph: CodeGraph,
) -> RetrievalResult:
    """Select entry and import-neighbor context for a newcomer reading order."""
    entry_nodes = find_entry_candidates(graph)
    selected_paths = [node.path for node in entry_nodes if node.path]
    if not selected_paths:
        selected_paths = [file_info.path for file_info in index.files[:3]]

    for path in list(selected_paths):
        for edge in get_file_imports(graph, path):
            target_path = _file_path_from_node_id(edge.target)
            if target_path is not None:
                selected_paths.append(target_path)

    items = [
        _context_for_file(
            path,
            project_path,
            index,
            graph,
            reason="入口候选或入口直接导入的文件，适合作为阅读顺序线索。",
        )
        for path in _dedupe([path for path in selected_paths if path])[:6]
    ]
    return RetrievalResult(
        question="生成项目新手阅读顺序",
        intent="onboard",
        items=items,
    )


def retrieve_impact(
    file_path: str,
    project_path: Path,
    index: ProjectIndex,
    graph: CodeGraph,
    question: str | None = None,
) -> RetrievalResult:
    """Return import and reverse-import context for a simple impact analysis."""
    normalized = _normalize_path(file_path)
    imported_by = get_file_imported_by(graph, normalized)
    imports = get_file_imports(graph, normalized)
    related_paths = [normalized]
    related_paths.extend(
        path
        for path in [_file_path_from_node_id(edge.source) for edge in imported_by]
        if path is not None
    )
    related_paths.extend(
        path
        for path in [_file_path_from_node_id(edge.target) for edge in imports]
        if path is not None
    )

    items = [
        _context_for_file(
            path,
            project_path,
            index,
            graph,
            reason="目标文件或通过 imports/imported-by 关系找到的影响范围候选。",
        )
        for path in _dedupe(related_paths)[:8]
    ]
    return RetrievalResult(
        question=question or f"分析修改 {normalized} 的影响范围",
        intent="impact",
        items=items,
    )


def _retrieve_entry(
    question: str,
    project_path: Path,
    index: ProjectIndex,
    graph: CodeGraph,
) -> RetrievalResult:
    items = [
        _context_for_file(
            node.path or node.name,
            project_path,
            index,
            graph,
            reason="图谱入口候选：文件名或 main 函数符合入口特征。",
        )
        for node in find_entry_candidates(graph)
        if node.path or node.name
    ]
    return RetrievalResult(question=question, intent="entry", items=items)


def _retrieve_dependency(
    question: str,
    project_path: Path,
    index: ProjectIndex,
    graph: CodeGraph,
) -> RetrievalResult:
    target = _best_file_match(question, index)
    if target is None:
        return _retrieve_keyword(question, project_path, index, graph)
    edges = get_file_imports(graph, target) + get_file_imported_by(graph, target)
    paths = [target]
    for edge in edges:
        for node_id in (edge.source, edge.target):
            path = _file_path_from_node_id(node_id)
            if path is not None:
                paths.append(path)
    items = [
        _context_for_file(
            path,
            project_path,
            index,
            graph,
            reason="与问题中目标文件存在导入或反向导入关系。",
        )
        for path in _dedupe(paths)[:6]
    ]
    return RetrievalResult(question=question, intent="dependency", items=items)


def _retrieve_keyword(
    question: str,
    project_path: Path,
    index: ProjectIndex,
    graph: CodeGraph,
) -> RetrievalResult:
    tokens = _tokens(question)
    scored: list[tuple[int, str]] = []
    for file_info in index.files:
        haystack = " ".join(
            [
                file_info.path,
                " ".join(file_info.imports),
                " ".join(file_info.functions),
                " ".join(class_info.name for class_info in file_info.classes),
                " ".join(
                    method
                    for class_info in file_info.classes
                    for method in class_info.methods
                ),
            ]
        ).lower()
        score = sum(1 for token in tokens if token in haystack)
        if score:
            scored.append((score, file_info.path))

    selected = [path for _, path in sorted(scored, reverse=True)[:6]]
    if not selected:
        selected = [file_info.path for file_info in index.files[:3]]

    items = [
        _context_for_file(
            path,
            project_path,
            index,
            graph,
            reason="根据问题关键词匹配到的文件。",
        )
        for path in selected
    ]
    return RetrievalResult(question=question, intent="general", items=items)


def _context_for_file(
    file_path: str,
    project_path: Path,
    index: ProjectIndex,
    graph: CodeGraph,
    reason: str,
) -> ContextItem:
    normalized = _normalize_path(file_path)
    file_info = _file_by_path(index).get(normalized)
    return ContextItem(
        title=f"File {normalized}",
        path=normalized,
        node_ids=_node_ids_for_path(graph, normalized),
        edges=_edge_descriptions(_edges_for_path(graph, normalized)),
        snippet=_read_snippet(project_path, normalized),
        reason=_file_summary(file_info) if file_info is not None else reason,
    )


def _detect_intent(question: str) -> str:
    text = question.lower()
    if any(word in text for word in ["入口", "启动", "entry", "main"]):
        return "entry"
    if any(word in text for word in ["影响", "impact", "改动", "修改"]):
        return "impact"
    if any(word in text for word in ["依赖", "import", "调用", "call"]):
        return "dependency"
    return "general"


def _best_file_match(question: str, index: ProjectIndex) -> str | None:
    normalized_question = _normalize_path(question).lower()
    for file_info in index.files:
        path = _normalize_path(file_info.path)
        if path.lower() in normalized_question:
            return path
    for file_info in index.files:
        name = _normalize_path(file_info.path).rsplit("/", 1)[-1]
        if name.lower() in normalized_question:
            return _normalize_path(file_info.path)
    return None


def _read_snippet(project_path: Path, relative_path: str) -> str:
    root = project_path.resolve()
    file_path = (root / relative_path).resolve()
    try:
        file_path.relative_to(root)
    except ValueError:
        return ""
    if not file_path.exists() or not file_path.is_file():
        return ""

    lines = file_path.read_text(encoding="utf-8-sig").splitlines()
    return "\n".join(
        f"{line_number}: {line}"
        for line_number, line in enumerate(lines[:MAX_SNIPPET_LINES], start=1)
    )


def _node_ids_for_path(graph: CodeGraph, path: str) -> list[str]:
    normalized = _normalize_path(path)
    return [
        node.id
        for node in graph.nodes
        if node.path == normalized or node.id == f"file:{normalized}"
    ]


def _edges_for_path(graph: CodeGraph, path: str) -> list[GraphEdge]:
    file_node_id = f"file:{_normalize_path(path)}"
    node_ids = {file_node_id}
    node_ids.update(_node_ids_for_path(graph, path))
    return [
        edge
        for edge in graph.edges
        if edge.source in node_ids or edge.target in node_ids
    ]


def _edge_descriptions(edges: list[GraphEdge]) -> list[str]:
    return [f"{edge.source} --{edge.type}--> {edge.target}" for edge in edges]


def _file_path_from_node_id(node_id: str) -> str | None:
    if not node_id.startswith("file:"):
        return None
    return node_id.removeprefix("file:")


def _file_by_path(index: ProjectIndex):
    return {_normalize_path(file_info.path): file_info for file_info in index.files}


def _file_summary(file_info) -> str:
    class_names = [class_info.name for class_info in file_info.classes]
    return (
        f"索引摘要：imports={len(file_info.imports)}, "
        f"classes={class_names}, functions={file_info.functions}, "
        f"has_main_guard={file_info.has_main_guard}。"
    )


def _tokens(text: str) -> list[str]:
    normalized = _normalize_path(text).lower()
    separators = " \t\r\n,.;:()[]{}<>\"'`，。！？、：；（）【】"
    for separator in separators:
        normalized = normalized.replace(separator, " ")
    return [token for token in normalized.split() if len(token) >= 2]


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/")


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
