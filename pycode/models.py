from dataclasses import dataclass, field


@dataclass
class ClassInfo:
    name: str
    methods: list[str] = field(default_factory=list)


@dataclass
class FileInfo:
    path: str
    imports: list[str] = field(default_factory=list)
    classes: list[ClassInfo] = field(default_factory=list)
    functions: list[str] = field(default_factory=list)


@dataclass
class ProjectIndex:
    project_path: str
    files: list[FileInfo] = field(default_factory=list)
