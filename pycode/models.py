from dataclasses import dataclass, field


@dataclass
class ClassInfo:
    name: str
    methods: list[str] = field(default_factory=list)


@dataclass
class CallInfo:
    caller: str
    calls: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ChunkSpan:
    """Transient lexical boundaries collected during the existing AST parse."""

    symbol_name: str
    qualified_name: str
    symbol_key: str
    symbol_kind: str
    class_name: str | None
    parent_symbol: str | None
    occurrence: int
    symbol_start_line: int
    symbol_end_line: int
    start_line: int
    end_line: int
    part_index: int
    part_count: int
    oversized: bool


@dataclass
class FileInfo:
    path: str
    imports: list[str] = field(default_factory=list)
    classes: list[ClassInfo] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    call_infos: list[CallInfo] = field(default_factory=list)
    has_main_guard: bool = False
    # Build-only data, deliberately excluded from the legacy Index JSON.
    source_text: str | None = field(default=None, compare=False, repr=False)
    chunk_spans: list[ChunkSpan] = field(default_factory=list, compare=False, repr=False)


@dataclass
class ProjectIndex:
    project_path: str
    files: list[FileInfo] = field(default_factory=list)


@dataclass
class GraphNode:
    id: str
    type: str
    name: str
    path: str | None = None


@dataclass
class GraphEdge:
    source: str
    target: str
    type: str


@dataclass
class CodeGraph:
    project_path: str
    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
