"""Snapshot evidence contracts and source reconstruction; no parsing or models."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePosixPath
from typing import Callable
from functools import cached_property

from pycode.chunks import ChunkIndex, SnapshotBinding, validate_chunk_index
from pycode.models import CodeGraph, ProjectIndex
from pycode.utils import dedupe_preserve_order, normalize_path, safe_read_text


@dataclass(frozen=True)
class SourceScore:
    source: str
    score: float
    rank: int
    contribution: float


@dataclass(frozen=True)
class EvidenceItem:
    # The legacy ContextItem constructor adapts to this same record.
    title: str
    path: str | None = None
    node_ids: tuple[str, ...] = ()
    edges: tuple[str, ...] = ()
    snippet: str = ""
    reason: str = ""
    project_id: str | None = None
    snapshot_id: str | None = None
    commit_sha: str | None = None
    symbol: str | None = None
    symbol_key: str | None = None
    symbol_kind: str | None = None
    class_name: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    chunk_ids: tuple[str, ...] = ()
    source: str = "legacy"
    score: float = 0.0
    role: str = "definition"
    coverage: str = "unavailable"
    diagnostics: tuple[str, ...] = ()
    source_scores: tuple[SourceScore, ...] = ()
    graph_path: tuple[dict, ...] = ()

    def to_dict(self) -> dict:
        value = asdict(self)
        for name in ("node_ids", "edges", "chunk_ids", "diagnostics", "source_scores", "graph_path"):
            value[name] = list(value[name])
        return value

    def reference(self) -> dict:
        value = self.to_dict()
        return {name: value[name] for name in (
            "path", "symbol", "start_line", "end_line", "chunk_ids", "source",
            "score", "reason", "role", "coverage", "diagnostics", "source_scores", "graph_path",
        )}


@dataclass(frozen=True)
class Query:
    text: str
    intent: str | None = None
    target: str | None = None


@dataclass(frozen=True)
class RetrievalRequest:
    query: Query
    limit: int = 6

    def __post_init__(self):
        if type(self.limit) is not int or not 1 <= self.limit <= 20:
            raise ValueError("Retrieval limit must be between 1 and 20.")


@dataclass(frozen=True)
class RetrievalResult:
    question: str
    intent: str
    items: list[EvidenceItem] = field(default_factory=list)
    binding: SnapshotBinding | None = None
    status: str = "ok"
    diagnostics: tuple[str, ...] = ()
    duration_ms: float = 0.0
    mode: str = "keyword"
    pack: object | None = None
    graph_stats: dict | None = None

    @property
    def evidence(self) -> list[str]:
        return dedupe_preserve_order(
            reference for item in self.items
            for reference in ([item.path] if item.path else []) + list(item.node_ids) + list(item.edges)
        )

    def summary(self) -> dict:
        return {
            "strategy": self.intent,
            "binding": asdict(self.binding) if self.binding else None,
            "status": self.status,
            "diagnostics": list(self.diagnostics),
            "duration_ms": self.duration_ms,
            "evidence_count": len(self.items),
            "references": [item.reference() for item in self.items],
            "mode": self.mode,
            "pack": self.pack.summary() if self.pack else None,
            "graph_stats": self.graph_stats,
        }


def safe_relative(path: str) -> str:
    path = normalize_path(path)
    if (not path or "\x00" in path or ":" in path or PurePosixPath(path).is_absolute()
            or any(part in {"", ".", ".."} for part in path.split("/"))):
        raise ValueError("Evidence path must be repository-relative.")
    return path


def source_lines(text: str) -> list[str]:
    # Match AST/Chunk LF line numbering, including Unicode separators in literals.
    parts = text.split("\n")
    return parts[:-1] if parts[-1] == "" else parts


@dataclass(frozen=True)
class RetrievalContext:
    """Trusted call-scoped input, frozen before entering a Tool or model.

    Chunk-backed sources come from published Artifacts, never a mutable checkout.
    Backend callers validate the bundle and exact Commit under the Project lock.
    """

    root: Path
    index: ProjectIndex
    graph: CodeGraph | None
    binding: SnapshotBinding | None
    chunks: ChunkIndex | None
    sources: tuple[tuple[str, str], ...]
    diagnostics: tuple[str, ...]
    vector_query: Callable | None = field(default=None, repr=False, compare=False)
    retrieval_mode: str = "keyword"
    vector_top_k: int = 6
    token_budget: int = 131072
    graph_depth: int = 2
    graph_nodes: int = 32
    graph_edge_checks: int = 128
    graph_direction: str = "both"

    @cached_property
    def graph_view(self):
        from pycode.query import RetrievalGraph
        return RetrievalGraph.create(self.graph) if self.graph is not None else None

    @classmethod
    def create(cls, root: Path, index: ProjectIndex, graph: CodeGraph | None, *,
               binding: SnapshotBinding | None = None, chunks: ChunkIndex | None = None):
        root = root.resolve()
        diagnostics = []
        if binding is None:
            diagnostics.append("snapshot_unbound")
        if graph is None:
            diagnostics.append("graph_missing")
        elif index.files and not graph.nodes:
            diagnostics.append("graph_incomplete")
        paths = {safe_relative(file.path) for file in index.files}
        sources = []
        if chunks is not None:
            if binding is None:
                raise ValueError("Chunk evidence requires a trusted Snapshot binding.")
            validate_chunk_index(chunks, binding)
            if paths != {file.path for file in chunks.files}:
                raise ValueError("Chunk and Index file sets differ.")
            by_file = {path: [] for path in paths}
            for chunk in sorted(chunks.chunks, key=lambda c: (c.path, c.span.start_line)):
                by_file[chunk.path].append(chunk.content)
            sources = [(path, "".join(by_file[path])) for path in sorted(paths)]
        else:
            diagnostics.append("chunks_missing")
            for path in sorted(paths):
                source = (root / path).resolve()
                if not source.is_relative_to(root):
                    diagnostics.append("source_path_denied")
                    continue
                try:
                    sources.append((path, safe_read_text(source)))
                except (OSError, UnicodeError):
                    diagnostics.append("source_unavailable")
        return cls(root, index, graph, binding, chunks, tuple(sources),
                   tuple(dedupe_preserve_order(diagnostics)))

    def item(self, path: str, *, reason: str, source: str, score: float = 0.0,
             span=None, role: str = "definition", include_graph: bool = True) -> EvidenceItem:
        path = safe_relative(path)
        text = dict(self.sources).get(path)
        lines = source_lines(text) if text is not None else []
        start, end = (span.symbol_start_line, span.symbol_end_line) if span else (1, len(lines))
        available = text is not None and 1 <= start <= end <= len(lines)
        chunks = tuple(chunk.id for chunk in (self.chunks.chunks if self.chunks else [])
                       if chunk.path == path and chunk.span.start_line <= end and chunk.span.end_line >= start)
        graph = (self.graph if include_graph else None) or CodeGraph(str(self.root))
        nodes = tuple(node.id for node in graph.nodes
                      if node.path == path or node.id == f"file:{path}")
        related = {f"file:{path}", *nodes}
        edges = tuple(f"{edge.source} --{edge.type}--> {edge.target}" for edge in graph.edges
                      if edge.source in related or edge.target in related)
        diagnostics = []
        if self.chunks is None:
            diagnostics.append("symbol_range_unavailable")
        if not available:
            diagnostics.append("source_unavailable" if text is None else "source_empty")
        binding = asdict(self.binding) if self.binding else {}
        return EvidenceItem(
            title=f"{span.qualified_name if span else 'File'} {path}", path=path,
            node_ids=nodes, edges=edges, reason=reason,
            snippet="\n".join(f"{number}: {lines[number - 1]}" for number in range(start, end + 1)) if available else "",
            **binding, symbol=span.qualified_name if span else None,
            symbol_key=span.symbol_key if span else None,
            symbol_kind=span.symbol_kind if span else None,
            class_name=span.class_name if span else None,
            start_line=start if available else None, end_line=end if available else None,
            chunk_ids=chunks, source=source, score=float(score), role=role,
            coverage=("complete_symbol" if span else "complete_file") if available else "unavailable",
            diagnostics=tuple(diagnostics),
        )
