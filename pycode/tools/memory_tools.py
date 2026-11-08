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
    id: str | None = None,
    memory_type: str | None = None,
    title: str = "",
    summary: str = "",
    body: str | None = None,
    tags: list[str] | str | None = None,
    confidence: float = 1.0,
    related_files: list[str] | str | None = None,
    query: str = "",
    limit: int = 5,
) -> ToolResult:
    """Manage project-local persistent memories under .pclens/memory."""
    try:
        return _memory(context, operation=operation, id=id, memory_type=memory_type,
            title=title, summary=summary, body=body, tags=tags, confidence=confidence,
            related_files=related_files, query=query, limit=limit)
    except (OSError, ValueError, UnicodeError, TypeError, KeyError):
        # Optional state must not invalidate an otherwise successful analysis.
        # ExecutionStopped intentionally bypasses this Exception boundary.
        return failure(TOOL_NAME, 'Memory operation unavailable.', 'memory_storage_unavailable',
                       storage_dir=_relative_storage_dir())


def _memory(context, *, operation, id, memory_type, title, summary, body, tags,
            confidence, related_files, query, limit):
    try:
        store = _store_from_context(context)
    except PermissionError as exc:
        return failure(TOOL_NAME, "Memory storage denied.", str(exc))

    if operation == "add":
        if not id:
            return _missing(operation, "id")
        if not memory_type:
            return _missing(operation, "memory_type")
        if body is None:
            return _missing(operation, "body")
        try:
            item = store.add_memory(
                memory_id=id,
                memory_type=memory_type,
                title=title,
                summary=summary,
                body=body,
                tags=_normalize_tags(tags),
                confidence=confidence,
                related_files=_normalize_list(related_files),
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
            f"Memory {item.id} created.",
            memory=item.to_dict(),
            diagnostics=list(store.diagnostics),
            evidence=[f"{DEFAULT_MEMORY_DIR}/{item.path}"],
            storage_dir=_relative_storage_dir(),
        )

    if operation == "list":
        entries = store.list_memories()
        return success(
            TOOL_NAME,
            f"Found {len(entries)} memories.",
            memories=[entry.to_dict() for entry in entries],
            diagnostics=list(store.diagnostics),
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
            diagnostics=list(store.diagnostics),
            evidence=[f"{DEFAULT_MEMORY_DIR}/{item.path}" for item in items],
            storage_dir=_relative_storage_dir(),
        )

    if operation == "load":
        if not id:
            return _missing(operation, "id")
        try:
            item = store.load_memory(id)
        except (FileNotFoundError, PermissionError, ValueError) as exc:
            return failure(
                TOOL_NAME,
                "Memory lookup failed.",
                str(exc),
                storage_dir=_relative_storage_dir(),
            )
        return success(
            TOOL_NAME,
            f"Memory {item.id} loaded.",
            memory=item.to_dict(),
            diagnostics=list(store.diagnostics),
            evidence=[f"{DEFAULT_MEMORY_DIR}/{item.path}"],
            storage_dir=_relative_storage_dir(),
        )

    if operation == "rebuild":
        entries = store.rebuild_index()
        return success(
            TOOL_NAME,
            f"Memory index rebuilt with {len(entries)} entries.",
            memories=[entry.to_dict() for entry in entries],
            diagnostics=list(store.diagnostics),
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
    from pycode.agent.memory_record import MemoryScope, effective_scope

    scope = effective_scope(context.project_root)
    if context.retrieval_context is not None and context.retrieval_context.root.resolve() != context.project_root:
        raise PermissionError('Memory tool workspace mismatch')
    if context.retrieval_context is not None and context.retrieval_context.binding is not None:
        binding = context.retrieval_context.binding
        if scope is not None and scope.binding != binding:
            raise PermissionError('Memory tool Snapshot mismatch')
        scope = scope or MemoryScope(context.project_root, binding)
    return MemoryStore(context.project_root, scope=scope, origin='model_inference')


def _relative_storage_dir() -> str:
    return DEFAULT_MEMORY_DIR


def _normalize_tags(value: list[str] | str | None) -> list[str]:
    return _normalize_list(value)


def _normalize_list(value: list[str] | str | None) -> list[str]:
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
