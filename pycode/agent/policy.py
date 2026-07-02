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
    if context.allow_tests:
        return None
    return failure(
        tool_name,
        "Tool execution denied.",
        "This tool is not read-only and tests were not explicitly allowed.",
    )
