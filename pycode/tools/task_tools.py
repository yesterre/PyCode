from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pycode.tools.base import ToolContext, ToolResult, failure, success

if TYPE_CHECKING:
    from pycode.agent.task_dag import TaskDAGStore


TOOL_NAME = "task_dag"


def task_dag(
    context: ToolContext,
    *,
    operation: str,
    task_id: str | None = None,
    title: str | None = None,
    description: str = "",
    owner: str | None = None,
    blocked_by: list[str] | str | None = None,
    metadata: dict[str, Any] | None = None,
) -> ToolResult:
    """Manage project-local Task DAG files under .pclens/tasks."""
    try:
        store = _store_from_context(context)
    except PermissionError as exc:
        return failure(TOOL_NAME, "Task storage denied.", str(exc))

    if operation == "create":
        if not title:
            return failure(
                TOOL_NAME,
                "Task title is required.",
                "operation='create' requires title.",
                storage_dir=_relative_storage_dir(context),
            )
        try:
            task = store.create_task(
                task_id=task_id,
                title=title,
                description=description,
                owner=owner,
                blocked_by=_normalize_blocked_by(blocked_by),
                metadata=metadata,
            )
        except (FileExistsError, PermissionError, ValueError) as exc:
            return failure(
                TOOL_NAME,
                "Task creation failed.",
                str(exc),
                storage_dir=_relative_storage_dir(context),
            )
        can_start = store.can_start(task)
        return success(
            TOOL_NAME,
            f"Task {task.id} created.",
            task=task.to_dict(),
            can_start=can_start.can_start,
            blocked_by=can_start.blocked_by,
            missing_dependencies=can_start.missing_dependencies,
            storage_dir=_relative_storage_dir(context),
        )

    if operation == "list":
        tasks = store.list_tasks()
        return success(
            TOOL_NAME,
            f"Found {len(tasks)} tasks.",
            tasks=[task.to_dict() for task in tasks],
            storage_dir=_relative_storage_dir(context),
        )

    if operation == "get":
        task = _require_task(store, task_id)
        if isinstance(task, ToolResult):
            return task
        can_start = store.can_start(task)
        return success(
            TOOL_NAME,
            f"Task {task.id} loaded.",
            task=task.to_dict(),
            can_start=can_start.can_start,
            blocked_by=can_start.blocked_by,
            missing_dependencies=can_start.missing_dependencies,
            storage_dir=_relative_storage_dir(context),
        )

    if operation == "claim":
        if not task_id:
            return _task_id_required()
        try:
            task = store.claim_task(task_id, owner=owner)
        except (FileNotFoundError, PermissionError, ValueError) as exc:
            return failure(
                TOOL_NAME,
                "Task claim failed.",
                str(exc),
                storage_dir=_relative_storage_dir(context),
            )
        can_start = store.can_start(task)
        return success(
            TOOL_NAME,
            f"Task {task.id} claimed.",
            task=task.to_dict(),
            can_start=can_start.can_start,
            blocked_by=can_start.blocked_by,
            storage_dir=_relative_storage_dir(context),
        )

    if operation == "complete":
        if not task_id:
            return _task_id_required()
        try:
            task, ready_tasks = store.complete_task(task_id)
        except (FileNotFoundError, PermissionError, ValueError) as exc:
            return failure(
                TOOL_NAME,
                "Task completion failed.",
                str(exc),
                storage_dir=_relative_storage_dir(context),
            )
        return success(
            TOOL_NAME,
            f"Task {task.id} completed.",
            task=task.to_dict(),
            ready_tasks=[task.to_dict() for task in ready_tasks],
            storage_dir=_relative_storage_dir(context),
        )

    return failure(
        TOOL_NAME,
        "Unsupported task operation.",
        f"Unsupported operation: {operation}",
        storage_dir=_relative_storage_dir(context),
    )


def _store_from_context(context: ToolContext) -> TaskDAGStore:
    from pycode.agent.task_dag import TaskDAGStore

    tasks_dir = context.resolve_in_project(".pclens/tasks")
    return TaskDAGStore(context.project_root, tasks_dir=tasks_dir)


def _relative_storage_dir(context: ToolContext) -> str:
    return ".pclens/tasks"


def _normalize_blocked_by(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item) for item in value]


def _require_task(store: TaskDAGStore, task_id: str | None) -> ToolResult | Any:
    if not task_id:
        return _task_id_required()
    try:
        return store.get_task(task_id)
    except (FileNotFoundError, PermissionError, ValueError) as exc:
        return failure(
            TOOL_NAME,
            "Task lookup failed.",
            str(exc),
            storage_dir=".pclens/tasks",
        )


def _task_id_required() -> ToolResult:
    return failure(
        TOOL_NAME,
        "Task id is required.",
        "This operation requires task_id.",
        storage_dir=".pclens/tasks",
    )
