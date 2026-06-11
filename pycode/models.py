from dataclasses import dataclass, field


@dataclass
class ClassInfo:
    name: str
    methods: list[str] = field(default_factory=list)


@dataclass
class CallInfo:
    caller: str
    calls: list[str] = field(default_factory=list)


@dataclass
class FileInfo:
    path: str
    imports: list[str] = field(default_factory=list)
    classes: list[ClassInfo] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)
    call_infos: list[CallInfo] = field(default_factory=list)
    has_main_guard: bool = False


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
