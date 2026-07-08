from pycode.constants import DEFAULT_ARTIFACT_DIR
from pycode.tools import ToolContext, ToolSpec, ToolResult
from pycode.tools.base import failure


def authorize_tool_call(
    tool_name: str,
    spec: ToolSpec,
    context: ToolContext,
) -> ToolResult | None:
    """Return a failure result when the tool call is not allowed."""
    if spec.read_only:
        return None
    if spec.writes_internal_state:
        try:
            context.resolve_in_project(DEFAULT_ARTIFACT_DIR)
        except PermissionError as exc:
            return failure(
                tool_name,
                "Tool execution denied.",
                str(exc),
                denied=True,
                denied_by="policy",
            )
        return None
    if context.allow_tests:
        return None
    return failure(
        tool_name,
        "Tool execution denied.",
        "This tool is not read-only and tests were not explicitly allowed.",
        denied=True,
        denied_by="policy",
    )
