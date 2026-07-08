from __future__ import annotations

from typing import TYPE_CHECKING

from pycode.constants import DEFAULT_MEMORY_DIR
from pycode.tools.base import ToolContext, ToolResult, failure, success

if TYPE_CHECKING:
    from pycode.agent.memory import MemoryStore


TOOL_NAME = "memory"


def memory(
    context: ToolContext,
    *,
    operation: str,
    name: str | None = None,
    memory_type: str | None = None,
    description: str = "",
    body: str | None = None,
    tags: list[str] | str | None = None,
    query: str = "",
    limit: int = 5,
) -> ToolResult:
    """Manage project-local persistent memories under .pclens/memory."""
    try:
        store = _store_from_context(context)
    except PermissionError as exc:
        return failure(TOOL_NAME, "Memory storage denied.", str(exc))

    if operation == "add":
        if not name:
            return _missing(operation, "name")
        if not memory_type:
            return _missing(operation, "memory_type")
        if body is None:
            return _missing(operation, "body")
        try:
            item = store.add_memory(
                name=name,
                memory_type=memory_type,
                description=description,
                body=body,
                tags=_normalize_tags(tags),
                source="manual",
            )
        except (PermissionError, ValueError) as exc:
            return failure(
                TOOL_NAME,
                "Memory creation failed.",
                str(exc),
                storage_dir=_relative_storage_dir(),
            )
        return success(
            TOOL_NAME,
            f"Memory {item.name} created.",
            memory=item.to_dict(),
            evidence=[f"{DEFAULT_MEMORY_DIR}/{item.path}"],
            storage_dir=_relative_storage_dir(),
        )

    if operation == "list":
        entries = store.list_memories()
        return success(
            TOOL_NAME,
            f"Found {len(entries)} memories.",
            memories=[entry.to_dict() for entry in entries],
            storage_dir=_relative_storage_dir(),
        )

    if operation == "search":
        try:
            items = store.search_memories(
                query,
                memory_type=memory_type,
                limit=limit,
                include_body=True,
            )
        except ValueError as exc:
            return failure(
                TOOL_NAME,
                "Memory search failed.",
                str(exc),
                storage_dir=_relative_storage_dir(),
            )
        return success(
            TOOL_NAME,
            f"Found {len(items)} matching memories.",
            memories=[item.to_dict() for item in items],
            evidence=[f"{DEFAULT_MEMORY_DIR}/{item.path}" for item in items],
            storage_dir=_relative_storage_dir(),
        )

    if operation == "load":
        if not name:
            return _missing(operation, "name")
        try:
            item = store.load_memory(name)
        except (FileNotFoundError, PermissionError, ValueError) as exc:
            return failure(
                TOOL_NAME,
                "Memory lookup failed.",
                str(exc),
                storage_dir=_relative_storage_dir(),
            )
        return success(
            TOOL_NAME,
            f"Memory {item.name} loaded.",
            memory=item.to_dict(),
            evidence=[f"{DEFAULT_MEMORY_DIR}/{item.path}"],
            storage_dir=_relative_storage_dir(),
        )

    if operation == "rebuild":
        entries = store.rebuild_index()
        return success(
            TOOL_NAME,
            f"Memory index rebuilt with {len(entries)} entries.",
            memories=[entry.to_dict() for entry in entries],
            evidence=[f"{DEFAULT_MEMORY_DIR}/MEMORY.md"],
            storage_dir=_relative_storage_dir(),
        )

    return failure(
        TOOL_NAME,
        "Unsupported memory operation.",
        f"Unsupported operation: {operation}",
        storage_dir=_relative_storage_dir(),
    )


def _store_from_context(context: ToolContext) -> MemoryStore:
    from pycode.agent.memory import MemoryStore

    memory_dir = context.resolve_in_project(DEFAULT_MEMORY_DIR)
    return MemoryStore(context.project_root, memory_dir=memory_dir)


def _relative_storage_dir() -> str:
    return DEFAULT_MEMORY_DIR


def _normalize_tags(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(item) for item in value]


def _missing(operation: str, field_name: str) -> ToolResult:
    return failure(
        TOOL_NAME,
        f"Memory {field_name} is required.",
        f"operation='{operation}' requires {field_name}.",
        storage_dir=_relative_storage_dir(),
    )
