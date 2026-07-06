from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from pycode.agent.types import AgentStep


class TodoStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


TodoStatus.ALL = {
    TodoStatus.PENDING,
    TodoStatus.IN_PROGRESS,
    TodoStatus.COMPLETED,
    TodoStatus.FAILED,
}


@dataclass
class TodoItem:
    id: str
    title: str
    tool: str
    reason: str = ""
    status: str = TodoStatus.PENDING
    error: str | None = None
    step_index: int | None = None
    required: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "tool": self.tool,
            "reason": self.reason,
            "status": self.status,
            "error": self.error,
            "step_index": self.step_index,
            "required": self.required,
        }


@dataclass
class TodoList:
    items: list[TodoItem] = field(default_factory=list)

    @classmethod
    def from_steps(cls, steps: list[AgentStep]) -> "TodoList":
        items: list[TodoItem] = []
        for index, step in enumerate(steps, start=1):
            todo_id = step.todo_id or f"todo-{index}"
            step.todo_id = todo_id
            items.append(
                TodoItem(
                    id=todo_id,
                    title=_title_for_step(step, index),
                    tool=step.tool,
                    reason=step.reason,
                    step_index=index,
                    required=step.required,
                )
            )
        return cls(items=items)

    def get(self, todo_id: str) -> TodoItem:
        for item in self.items:
            if item.id == todo_id:
                return item
        raise ValueError(f"Unknown todo id: {todo_id}")

    def to_dict(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self.items]

    def summary(self) -> dict[str, Any]:
        counts = {
            TodoStatus.PENDING: 0,
            TodoStatus.IN_PROGRESS: 0,
            TodoStatus.COMPLETED: 0,
            TodoStatus.FAILED: 0,
        }
        for item in self.items:
            counts[item.status] = counts.get(item.status, 0) + 1
        in_progress = next(
            (item.id for item in self.items if item.status == TodoStatus.IN_PROGRESS),
            None,
        )
        return {
            "total": len(self.items),
            "pending": counts[TodoStatus.PENDING],
            "in_progress": counts[TodoStatus.IN_PROGRESS],
            "completed": counts[TodoStatus.COMPLETED],
            "failed": counts[TodoStatus.FAILED],
            "current": in_progress,
        }


class TodoManager:
    def __init__(self, todo_list: TodoList) -> None:
        self.todo_list = todo_list

    @classmethod
    def from_steps(cls, steps: list[AgentStep]) -> "TodoManager":
        return cls(TodoList.from_steps(steps))

    @property
    def items(self) -> list[TodoItem]:
        return self.todo_list.items

    def start(self, todo_id: str) -> TodoItem:
        item = self.todo_list.get(todo_id)
        self._ensure_valid_transition(item, TodoStatus.IN_PROGRESS)
        active = [
            todo
            for todo in self.items
            if todo.status == TodoStatus.IN_PROGRESS and todo.id != todo_id
        ]
        if active:
            raise ValueError(
                f"Cannot start {todo_id}; {active[0].id} is already in progress."
            )
        item.status = TodoStatus.IN_PROGRESS
        item.error = None
        return item

    def complete(self, todo_id: str) -> TodoItem:
        item = self.todo_list.get(todo_id)
        self._ensure_valid_transition(item, TodoStatus.COMPLETED)
        item.status = TodoStatus.COMPLETED
        item.error = None
        return item

    def fail(self, todo_id: str, error: str | None = None) -> TodoItem:
        item = self.todo_list.get(todo_id)
        self._ensure_valid_transition(item, TodoStatus.FAILED)
        item.status = TodoStatus.FAILED
        item.error = error
        return item

    def set_status(
        self,
        todo_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> TodoItem:
        if status not in TodoStatus.ALL:
            raise ValueError(f"Unsupported todo status: {status}")
        if status == TodoStatus.IN_PROGRESS:
            return self.start(todo_id)
        if status == TodoStatus.COMPLETED:
            return self.complete(todo_id)
        if status == TodoStatus.FAILED:
            return self.fail(todo_id, error)
        item = self.todo_list.get(todo_id)
        self._ensure_valid_transition(item, TodoStatus.PENDING)
        item.status = TodoStatus.PENDING
        item.error = None
        return item

    def to_dict(self) -> list[dict[str, Any]]:
        return self.todo_list.to_dict()

    def summary(self) -> dict[str, Any]:
        return self.todo_list.summary()

    def _ensure_valid_transition(self, item: TodoItem, next_status: str) -> None:
        if next_status not in TodoStatus.ALL:
            raise ValueError(f"Unsupported todo status: {next_status}")
        if item.status == TodoStatus.COMPLETED and next_status != TodoStatus.COMPLETED:
            raise ValueError(f"Completed todo cannot move to {next_status}: {item.id}")
        if next_status == TodoStatus.PENDING and item.status != TodoStatus.PENDING:
            raise ValueError(f"Todo cannot return to pending: {item.id}")


def _title_for_step(step: AgentStep, index: int) -> str:
    if step.reason:
        return step.reason
    return f"Step {index}: {step.tool}"
