from dataclasses import asdict, dataclass, field, replace
import math
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from pycode.retrieval_evidence import RetrievalContext


@dataclass
class ToolContext:
    project_path: Path
    allow_tests: bool = False
    python_executable: str | None = None
    max_output_chars: int = 4000
    state: dict[str, Any] = field(default_factory=dict)
    retrieval_context: "RetrievalContext | None" = None
    initial_evidence: Any = field(default=None, repr=False)

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
    execution: "ToolExecution | None" = None

    @property
    def safe_summary(self) -> str:
        return TOOL_MESSAGES[self.execution.category] if self.execution is not None and not self.ok else self.summary

    @property
    def safe_error(self) -> str | None:
        return self.safe_summary if not self.ok and self.execution is not None else self.error


TOOL_MESSAGES = {
    'success': 'Tool completed.', 'empty': 'Tool returned no matching data.',
    'unknown_tool': 'Unknown tool.', 'invalid_arguments': 'Tool arguments are invalid.',
    'permission_denied': 'Tool execution denied.', 'not_found': 'Tool target was not found.',
    'timeout': 'Tool execution timed out.', 'internal_error': 'Tool execution raised an exception.',
    'transient_io': 'Tool encountered a transient I/O error.', 'test_failed': 'Tests did not pass.',
    'failed': 'Tool did not complete successfully.',
}


@dataclass(frozen=True)
class ToolExecution:
    category: str
    attempts: int = 1
    retryable: bool = False
    truncated: bool = False
    schema_version: int = 1

    def __post_init__(self):
        if (self.category not in TOOL_MESSAGES or type(self.attempts) is not int or not 0 <= self.attempts <= 2
                or type(self.retryable) is not bool or type(self.truncated) is not bool
                or type(self.schema_version) is not int or self.schema_version != 1):
            raise ValueError('Invalid tool execution summary')

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: Callable[..., ToolResult]
    read_only: bool = True
    writes_internal_state: bool = False
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=dict)
    examples: list[dict[str, Any]] = field(default_factory=list)
    requires_confirmation: bool = False
    destructive: bool = False
    allowed_in_plan_only: bool = True
    category: str = "general"
    output_summary_policy: str = "summary"
    max_attempts: int = 1
    retry_on: tuple[str, ...] = ()

    def __post_init__(self):
        if (type(self.max_attempts) is not int or not 1 <= self.max_attempts <= 2
                or any(code not in {'transient_io', 'timeout'} for code in self.retry_on)
                or (self.max_attempts > 1 and (not self.read_only or self.writes_internal_state
                    or self.destructive or self.requires_confirmation))):
            raise ValueError('Invalid tool retry policy')


def success(tool: str, summary: str, **data: Any) -> ToolResult:
    return ToolResult(tool=tool, ok=True, summary=summary, data=data)


def failure(tool: str, summary: str, error: str, **data: Any) -> ToolResult:
    return ToolResult(tool=tool, ok=False, summary=summary, error=error, data=data)


def normalize_tool_result(result, tool, *, attempts=1, category=None, retryable=False):
    """Keep legacy private diagnostics; new projections use only bounded categories."""
    if (not isinstance(result, ToolResult) or result.tool != tool or type(result.ok) is not bool
            or not isinstance(result.summary, str) or not isinstance(result.data, dict)
            or (result.error is not None and (not isinstance(result.error, str) or result.ok))
            or (result.execution is not None and not isinstance(result.execution, ToolExecution))):
        result = failure(tool, TOOL_MESSAGES['internal_error'], 'Invalid tool result.')
        category = 'internal_error'
    if category is None and result.execution is not None:
        category = result.execution.category
    if category is None:
        if result.ok:
            values = [value for key, value in result.data.items()
                      if key in {'items','matches','files','nodes','edges','evidence','content'}
                      and isinstance(value, (str,list,tuple))]
            category = 'empty' if values and not any(values) else 'success'
        elif result.data.get('denied'):
            category = 'permission_denied'
        elif result.data.get('timed_out'):
            category = 'timeout'
        elif result.tool == 'run_tests' and result.data.get('exit_code') not in (None,0):
            category = 'test_failed'
        else:
            category = 'failed'
    if result.ok != (category in {'success','empty'}):
        result = failure(tool, TOOL_MESSAGES['internal_error'], 'Inconsistent tool result.')
        category = 'internal_error'
    if category == 'permission_denied':
        result = replace(result, data={**result.data, 'denied': True,
            'denied_by': result.data.get('denied_by') or 'other'})
    truncated = any(result.data.get(k) is True for k in ('truncated','stdout_truncated','stderr_truncated'))
    return replace(result, execution=ToolExecution(category, attempts, retryable, truncated))


def validate_tool_arguments(spec: ToolSpec, arguments: dict[str, Any]) -> str | None:
    """Validate model-planned tool arguments against the registered schema."""
    if not isinstance(arguments, dict) or any(not isinstance(k,str) for k in arguments):
        return 'Tool arguments must be an object with string keys.'
    schema = spec.input_schema or {}
    try:
        return _validate_schema(arguments, schema, 'arguments', 0)
    except (TypeError, ValueError, OverflowError):
        return 'Unsupported tool argument schema.'


def _validate_schema(value, schema, location, depth):
    if not isinstance(schema, dict) or depth > 16:
        return 'Unsupported tool argument schema.'
    types = schema.get('type')
    if types is not None:
        types = [types] if isinstance(types,str) else types
        checks = {'null': lambda v:v is None, 'string': lambda v:isinstance(v,str),
            'boolean': lambda v:type(v) is bool, 'integer': lambda v:type(v) is int,
            'number': lambda v:type(v) in (int,float) and math.isfinite(v),
            'array': lambda v:isinstance(v,list), 'object': lambda v:isinstance(v,dict)}
        if not isinstance(types,list) or not types or any(t not in checks for t in types):
            return 'Unsupported tool argument schema.'
        if not any(checks[t](value) for t in types):
            return f'Invalid type for {location}.'
    if 'enum' in schema and not any(type(value) is type(item) and value == item for item in schema['enum']):
        return f'Invalid enum for {location}.'
    if type(value) in (int,float):
        if (not math.isfinite(value) or ('minimum' in schema and value < schema['minimum'])
                or ('maximum' in schema and value > schema['maximum'])
                or ('exclusiveMinimum' in schema and value <= schema['exclusiveMinimum'])
                or ('exclusiveMaximum' in schema and value >= schema['exclusiveMaximum'])):
            return f'Numeric boundary exceeded for {location}.'
    if isinstance(value,str):
        if len(value) < schema.get('minLength',0) or len(value) > schema.get('maxLength',float('inf')):
            return f'String boundary exceeded for {location}.'
    if isinstance(value,list):
        if len(value) < schema.get('minItems',0) or len(value) > schema.get('maxItems',float('inf')):
            return f'Array boundary exceeded for {location}.'
        for item in value:
            if error := _validate_schema(item,schema.get('items',{}),location+'[]',depth+1):
                return error
    if isinstance(value,dict):
        properties, required = schema.get('properties',{}), schema.get('required',[])
        if not isinstance(properties,dict) or not isinstance(required,list):
            return 'Unsupported tool argument schema.'
        if any(key not in value for key in required):
            return 'Missing required argument(s).'
        if (properties or schema.get('additionalProperties') is False) and any(key not in properties for key in value):
            return 'Unknown argument(s).'
        for key, item in value.items():
            if not isinstance(key,str):
                return 'Tool arguments must have string keys.'
            if error := _validate_schema(item,properties.get(key,{}),location+'.'+key if key in properties else location,depth+1):
                return error
    return None


def truncate_text(text: str, max_chars: int) -> tuple[str, bool]:
    if max_chars < 0:
        max_chars = 0
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True
