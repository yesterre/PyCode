from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pycode.agent._time_utils import format_timestamp, utc_now
from pycode.constants import DEFAULT_TASK_DIR
from pycode.utils import ensure_directory

TASK_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
GENERATED_TASK_ID_PATTERN = re.compile(r"^task_(\d+)$")


class TaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


TaskStatus.ALL = {
    TaskStatus.PENDING,
    TaskStatus.IN_PROGRESS,
    TaskStatus.COMPLETED,
}


@dataclass
class TaskNode:
    id: str
    title: str
    description: str = ""
    status: str = TaskStatus.PENDING
    owner: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: format_timestamp(utc_now()))
    updated_at: str = field(default_factory=lambda: format_timestamp(utc_now()))
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "owner": self.owner,
            "blocked_by": list(self.blocked_by),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskNode":
        status = data.get("status", TaskStatus.PENDING)
        if status not in TaskStatus.ALL:
            raise ValueError(f"Unsupported task status: {status}")
        return cls(
            id=str(data["id"]),
            title=str(data.get("title", "")),
            description=str(data.get("description", "")),
            status=status,
            owner=data.get("owner"),
            blocked_by=[str(item) for item in data.get("blocked_by", [])],
            created_at=str(data.get("created_at") or format_timestamp(utc_now())),
            updated_at=str(data.get("updated_at") or format_timestamp(utc_now())),
            metadata=dict(data.get("metadata", {})),
        )


@dataclass
class CanStartResult:
    can_start: bool
    blocked_by: list[str] = field(default_factory=list)
    missing_dependencies: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "can_start": self.can_start,
            "blocked_by": list(self.blocked_by),
            "missing_dependencies": list(self.missing_dependencies),
        }


class TaskDAGStore:
    def __init__(
        self,
        project_path: str | Path,
        tasks_dir: str | Path | None = None,
    ) -> None:
        self.project_path = Path(project_path).resolve()
        if tasks_dir is None:
            self.tasks_dir = (self.project_path / DEFAULT_TASK_DIR).resolve()
        else:
            candidate = Path(tasks_dir)
            if candidate.is_absolute():
                self.tasks_dir = candidate.resolve()
            else:
                self.tasks_dir = (self.project_path / candidate).resolve()
        self._ensure_tasks_dir_in_project()

    def create_task(
        self,
        *,
        title: str,
        description: str = "",
        task_id: str | None = None,
        owner: str | None = None,
        blocked_by: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskNode:
        if not title:
            raise ValueError("Task title is required.")
        actual_id = task_id or self._next_task_id()
        self._validate_task_id(actual_id)
        task_path = self._task_path(actual_id)
        if task_path.exists():
            raise FileExistsError(f"Task already exists: {actual_id}")
        node = TaskNode(
            id=actual_id,
            title=title,
            description=description,
            owner=owner,
            blocked_by=[str(item) for item in blocked_by or []],
            metadata=dict(metadata or {}),
        )
        self._save_task(node)
        return node

    def list_tasks(self) -> list[TaskNode]:
        if not self.tasks_dir.exists():
            return []
        tasks = [
            self._load_task(path)
            for path in self.tasks_dir.glob("*.json")
            if path.is_file()
        ]
        return sorted(tasks, key=lambda task: task.id)

    def get_task(self, task_id: str) -> TaskNode:
        self._validate_task_id(task_id)
        task_path = self._task_path(task_id)
        if not task_path.exists():
            raise FileNotFoundError(f"Task does not exist: {task_id}")
        return self._load_task(task_path)

    def can_start(self, task_or_id: TaskNode | str) -> CanStartResult:
        task = self.get_task(task_or_id) if isinstance(task_or_id, str) else task_or_id
        blocked_by: list[str] = []
        missing_dependencies: list[str] = []
        for dependency_id in task.blocked_by:
            try:
                dependency = self.get_task(dependency_id)
            except FileNotFoundError:
                missing_dependencies.append(dependency_id)
                continue
            if dependency.status != TaskStatus.COMPLETED:
                blocked_by.append(dependency_id)
        return CanStartResult(
            can_start=not blocked_by and not missing_dependencies,
            blocked_by=blocked_by,
            missing_dependencies=missing_dependencies,
        )

    def claim_task(self, task_id: str, *, owner: str | None = None) -> TaskNode:
        task = self.get_task(task_id)
        if task.status == TaskStatus.COMPLETED:
            raise ValueError(f"Completed task cannot be claimed: {task_id}")
        if task.status == TaskStatus.IN_PROGRESS:
            raise ValueError(f"Task is already in progress: {task_id}")
        can_start = self.can_start(task)
        if not can_start.can_start:
            blocked = can_start.blocked_by + can_start.missing_dependencies
            raise ValueError(
                f"Task is blocked and cannot be claimed: {task_id}. "
                f"blocked_by={blocked}"
            )
        task.status = TaskStatus.IN_PROGRESS
        task.owner = owner if owner is not None else task.owner
        self._touch(task)
        self._save_task(task)
        return task

    def complete_task(self, task_id: str) -> tuple[TaskNode, list[TaskNode]]:
        task = self.get_task(task_id)
        if task.status == TaskStatus.COMPLETED:
            raise ValueError(f"Task is already completed: {task_id}")
        if task.status != TaskStatus.IN_PROGRESS:
            raise ValueError(f"Task must be in progress before completion: {task_id}")
        task.status = TaskStatus.COMPLETED
        self._touch(task)
        self._save_task(task)
        ready_tasks = [
            candidate
            for candidate in self.list_tasks()
            if candidate.status == TaskStatus.PENDING
            and task.id in candidate.blocked_by
            and self.can_start(candidate).can_start
        ]
        return task, ready_tasks

    def _task_path(self, task_id: str) -> Path:
        self._validate_task_id(task_id)
        path = (self.tasks_dir / f"{task_id}.json").resolve()
        try:
            path.relative_to(self.tasks_dir)
        except ValueError as exc:
            raise PermissionError(f"Task path escaped tasks directory: {task_id}") from exc
        return path

    def _load_task(self, path: Path) -> TaskNode:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        task = TaskNode.from_dict(data)
        self._validate_task_id(task.id)
        return task

    def _save_task(self, task: TaskNode) -> None:
        self._validate_task_id(task.id)
        if task.status not in TaskStatus.ALL:
            raise ValueError(f"Unsupported task status: {task.status}")
        ensure_directory(self.tasks_dir)
        self._task_path(task.id).write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _next_task_id(self) -> str:
        max_number = 0
        for task in self.list_tasks():
            match = GENERATED_TASK_ID_PATTERN.match(task.id)
            if match:
                max_number = max(max_number, int(match.group(1)))
        return f"task_{max_number + 1:03d}"

    def _ensure_tasks_dir_in_project(self) -> None:
        try:
            self.tasks_dir.relative_to(self.project_path)
        except ValueError as exc:
            raise PermissionError(
                f"Task storage directory is outside the project: {self.tasks_dir}"
            ) from exc

    @staticmethod
    def _validate_task_id(task_id: str) -> None:
        if not task_id:
            raise ValueError("Task id is required.")
        if not TASK_ID_PATTERN.match(task_id):
            raise ValueError(f"Unsupported task id: {task_id}")
        if task_id in {".", ".."}:
            raise ValueError(f"Unsupported task id: {task_id}")

    @staticmethod
    def _touch(task: TaskNode) -> None:
        task.updated_at = format_timestamp(utc_now())
