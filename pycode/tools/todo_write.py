from __future__ import annotations

from typing import TYPE_CHECKING

from pycode.tools.base import ToolContext, ToolResult, failure, success

if TYPE_CHECKING:
    from pycode.agent.todo import TodoManager


TOOL_NAME = "todo_write"


def todo_write(
    context: ToolContext,
    *,
    operation: str = "list",
    todo_id: str | None = None,
    status: str | None = None,
    error: str | None = None,
) -> ToolResult:
    """Read or update the in-memory Agent todo list for the current runtime."""
    from pycode.agent.todo import TodoManager

    manager = context.state.get("todo_manager")
    if not isinstance(manager, TodoManager):
        return failure(
            TOOL_NAME,
            "Todo manager is unavailable.",
            "todo_write requires an active Agent runtime todo manager.",
        )

    if operation == "list":
        return success(
            TOOL_NAME,
            "Todo list collected.",
            todos=manager.to_dict(),
            progress=manager.summary(),
        )

    if operation == "set_status":
        if not todo_id:
            return failure(
                TOOL_NAME,
                "Todo id is required.",
                "operation='set_status' requires todo_id.",
            )
        if not status:
            return failure(
                TOOL_NAME,
                "Todo status is required.",
                "operation='set_status' requires status.",
            )
        try:
            item = manager.set_status(todo_id, status, error=error)
        except ValueError as exc:
            return failure(
                TOOL_NAME,
                "Todo status update failed.",
                str(exc),
            )
        return success(
            TOOL_NAME,
            f"Todo {todo_id} updated to {item.status}.",
            todo=item.to_dict(),
            todos=manager.to_dict(),
            progress=manager.summary(),
        )

    return failure(
        TOOL_NAME,
        "Unsupported todo operation.",
        f"Unsupported operation: {operation}",
    )
