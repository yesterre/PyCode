from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class ToolContext:
    project_path: Path
    allow_tests: bool = False
    python_executable: str | None = None
    max_output_chars: int = 4000
    state: dict[str, Any] = field(default_factory=dict)

    @property
    def project_root(self) -> Path:
        return Path(self.project_path).resolve()

    def resolve_in_project(self, path: str | Path) -> Path:
        candidate = Path(path)
        if candidate.is_absolute():
            resolved = candidate.resolve()
        else:
            resolved = (self.project_root / candidate).resolve()

        try:
            resolved.relative_to(self.project_root)
        except ValueError as exc:
            raise PermissionError(
                f"Path is outside the project directory: {path}"
            ) from exc
        return resolved

    def relative_path(self, path: str | Path) -> str:
        resolved = self.resolve_in_project(path)
        return resolved.relative_to(self.project_root).as_posix()


@dataclass
class ToolResult:
    tool: str
    ok: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: Callable[..., ToolResult]
    read_only: bool = True
    writes_internal_state: bool = False
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    examples: list[dict[str, Any]] = field(default_factory=list)


def success(tool: str, summary: str, **data: Any) -> ToolResult:
    return ToolResult(tool=tool, ok=True, summary=summary, data=data)


def failure(tool: str, summary: str, error: str, **data: Any) -> ToolResult:
    return ToolResult(tool=tool, ok=False, summary=summary, error=error, data=data)


def validate_tool_arguments(spec: ToolSpec, arguments: dict[str, Any]) -> str | None:
    """Validate model-planned tool arguments against the registered schema."""
    schema = spec.input_schema or {}
    properties = schema.get("properties", {})
    required = schema.get("required", [])

    if not isinstance(properties, dict):
        properties = {}
    if not isinstance(required, list):
        required = []

    missing = [
        str(name)
        for name in required
        if isinstance(name, str) and name not in arguments
    ]
    if missing:
        return f"Missing required argument(s): {', '.join(missing)}"

    if properties:
        allowed = {str(name) for name in properties}
        unknown = sorted(set(arguments) - allowed)
        if unknown:
            return f"Unknown argument(s): {', '.join(unknown)}"

    return None


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars < 0:
        max_chars = 0
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True
