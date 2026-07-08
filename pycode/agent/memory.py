from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pycode.agent._time_utils import format_timestamp, utc_now
from pycode.constants import DEFAULT_MEMORY_DIR
from pycode.tools.base import ToolResult
from pycode.utils import dedupe_preserve_order, ensure_directory, parse_json_array_response


MEMORY_INDEX_FILE = "MEMORY.md"
MEMORY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_RELEVANT_MEMORIES = 5


class MemoryType(StrEnum):
    USER = "user"
    FEEDBACK = "feedback"
    PROJECT = "project"
    REFERENCE = "reference"


MemoryType.ALL = {
    MemoryType.USER,
    MemoryType.FEEDBACK,
    MemoryType.PROJECT,
    MemoryType.REFERENCE,
}


@dataclass
class MemoryIndexEntry:
    name: str
    type: str
    description: str = ""
    path: str = ""
    tags: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "path": self.path,
            "tags": list(self.tags),
        }


@dataclass
class MemoryItem:
    name: str
    type: str
    description: str
    body: str
    tags: list[str] = field(default_factory=list)
    source: str = "manual"
    created_at: str = field(default_factory=lambda: format_timestamp(utc_now()))
    updated_at: str = field(default_factory=lambda: format_timestamp(utc_now()))
    path: str = ""

    def to_index_entry(self) -> MemoryIndexEntry:
        return MemoryIndexEntry(
            name=self.name,
            type=self.type,
            description=self.description,
            path=self.path or f"{self.name}.md",
            tags=list(self.tags),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": self.type,
            "description": self.description,
            "body": self.body,
            "tags": list(self.tags),
            "source": self.source,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "path": self.path,
        }


@dataclass
class MemoryRunInfo:
    index_entries: list[MemoryIndexEntry] = field(default_factory=list)
    relevant_memories: list[MemoryItem] = field(default_factory=list)
    extracted_memories: list[MemoryItem] = field(default_factory=list)
    selection_error: str | None = None
    extraction_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index_entries": [entry.to_dict() for entry in self.index_entries],
            "relevant_memories": [item.to_dict() for item in self.relevant_memories],
            "extracted_memories": [item.to_dict() for item in self.extracted_memories],
            "selection_error": self.selection_error,
            "extraction_error": self.extraction_error,
        }

    def summary(self) -> dict[str, Any]:
        return {
            "index_entries": len(self.index_entries),
            "relevant_memories": len(self.relevant_memories),
            "extracted_memories": len(self.extracted_memories),
            "selection_error": self.selection_error,
            "extraction_error": self.extraction_error,
        }


class MemoryStore:
    def __init__(
        self,
        project_path: str | Path,
        memory_dir: str | Path | None = None,
    ) -> None:
        self.project_path = Path(project_path).resolve()
        if memory_dir is None:
            self.memory_dir = (self.project_path / DEFAULT_MEMORY_DIR).resolve()
        else:
            candidate = Path(memory_dir)
            if candidate.is_absolute():
                self.memory_dir = candidate.resolve()
            else:
                self.memory_dir = (self.project_path / candidate).resolve()
        self._ensure_memory_dir_in_project()

    @property
    def index_path(self) -> Path:
        return self.memory_dir / MEMORY_INDEX_FILE

    def add_memory(
        self,
        *,
        name: str,
        memory_type: str,
        description: str,
        body: str,
        tags: list[str] | None = None,
        source: str = "manual",
        allow_existing: bool = False,
    ) -> MemoryItem:
        if not name:
            raise ValueError("Memory name is required.")
        if not body:
            raise ValueError("Memory body is required.")
        self._validate_memory_type(memory_type)

        actual_name = self._slugify(name)
        if not allow_existing:
            actual_name = self._unique_name(actual_name)

        existing = self._memory_path(actual_name)
        created_at = format_timestamp(utc_now())
        if allow_existing and existing.exists():
            old = self.load_memory(actual_name)
            created_at = old.created_at

        entries = [
            entry
            for entry in self.list_memories()
            if entry.name != actual_name
        ]
        item = MemoryItem(
            name=actual_name,
            type=memory_type,
            description=description,
            body=body,
            tags=[str(tag) for tag in tags or []],
            source=source,
            created_at=created_at,
            updated_at=format_timestamp(utc_now()),
            path=f"{actual_name}.md",
        )
        self._save_memory(item)
        entries.append(item.to_index_entry())
        self._write_index(entries)
        return item

    def list_memories(self) -> list[MemoryIndexEntry]:
        if not self.memory_dir.exists():
            return []
        entries = [
            self._load_memory(path).to_index_entry()
            for path in self.memory_dir.glob("*.md")
            if path.is_file() and path.name != MEMORY_INDEX_FILE
        ]
        return sorted(entries, key=lambda entry: (entry.type, entry.name))

    def search_memories(
        self,
        query: str = "",
        *,
        memory_type: str | None = None,
        limit: int = MAX_RELEVANT_MEMORIES,
        include_body: bool = False,
    ) -> list[MemoryItem]:
        if memory_type:
            self._validate_memory_type(memory_type)
        terms = _query_terms(query)
        scored: list[tuple[int, MemoryItem]] = []
        for entry in self.list_memories():
            if memory_type and entry.type != memory_type:
                continue
            item = self.load_memory(entry.name)
            haystack = " ".join(
                [
                    item.name,
                    item.type,
                    item.description,
                    " ".join(item.tags),
                    item.body if include_body else "",
                ]
            ).lower()
            score = sum(1 for term in terms if term in haystack)
            if not terms:
                score = 1
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], pair[1].name))
        return [item for _, item in scored[: max(limit, 0)]]

    def load_memory(self, name: str) -> MemoryItem:
        path = self._memory_path(self._slugify(name))
        if not path.exists():
            raise FileNotFoundError(f"Memory does not exist: {name}")
        return self._load_memory(path)

    def rebuild_index(self) -> list[MemoryIndexEntry]:
        entries = self.list_memories()
        self._write_index(entries)
        return entries

    def _write_index(self, entries: list[MemoryIndexEntry]) -> None:
        ensure_directory(self.memory_dir)
        sorted_entries = sorted(entries, key=lambda entry: (entry.type, entry.name))
        lines = [
            "# PyCode Memory Index",
            "",
            "This file is generated from `.pclens/memory/*.md`.",
            "",
        ]
        for memory_type in [MemoryType.USER, MemoryType.FEEDBACK, MemoryType.PROJECT, MemoryType.REFERENCE]:
            typed_entries = [entry for entry in sorted_entries if entry.type == memory_type]
            if not typed_entries:
                continue
            lines.append(f"## {memory_type}")
            for entry in typed_entries:
                tags = f" tags={','.join(entry.tags)}" if entry.tags else ""
                lines.append(
                    f"- [{entry.name}]({entry.path}) - {entry.description}{tags}"
                )
            lines.append("")
        self.index_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")

    def read_index_text(self) -> str:
        if not self.index_path.exists():
            return ""
        return self.index_path.read_text(encoding="utf-8-sig").strip()

    def _memory_path(self, name: str) -> Path:
        self._validate_memory_name(name)
        path = (self.memory_dir / f"{name}.md").resolve()
        try:
            path.relative_to(self.memory_dir)
        except ValueError as exc:
            raise PermissionError(f"Memory path escaped memory directory: {name}") from exc
        return path

    def _load_memory(self, path: Path) -> MemoryItem:
        metadata, body = _parse_memory_file(path.read_text(encoding="utf-8-sig"))
        memory_type = str(metadata.get("type", ""))
        self._validate_memory_type(memory_type)
        name = str(metadata.get("name") or path.stem)
        self._validate_memory_name(name)
        return MemoryItem(
            name=name,
            type=memory_type,
            description=str(metadata.get("description") or ""),
            body=body.strip(),
            tags=[str(tag) for tag in metadata.get("tags", [])],
            source=str(metadata.get("source") or "manual"),
            created_at=str(metadata.get("created_at") or format_timestamp(utc_now())),
            updated_at=str(metadata.get("updated_at") or format_timestamp(utc_now())),
            path=path.name,
        )

    def _save_memory(self, item: MemoryItem) -> None:
        self._validate_memory_name(item.name)
        self._validate_memory_type(item.type)
        ensure_directory(self.memory_dir)
        self._memory_path(item.name).write_text(_format_memory_file(item), encoding="utf-8")

    def _unique_name(self, base_name: str) -> str:
        if not self._memory_path(base_name).exists():
            return base_name
        number = 2
        while self._memory_path(f"{base_name}-{number}").exists():
            number += 1
        return f"{base_name}-{number}"

    def _ensure_memory_dir_in_project(self) -> None:
        try:
            self.memory_dir.relative_to(self.project_path)
        except ValueError as exc:
            raise PermissionError(
                f"Memory storage directory is outside the project: {self.memory_dir}"
            ) from exc

    @staticmethod
    def _slugify(value: str) -> str:
        text = value.strip().lower()
        text = re.sub(r"[^a-z0-9_.-]+", "-", text)
        text = re.sub(r"-+", "-", text).strip("-._")
        if not text:
            raise ValueError(f"Unsupported memory name: {value}")
        MemoryStore._validate_memory_name(text)
        return text

    @staticmethod
    def _validate_memory_name(name: str) -> None:
        if not name:
            raise ValueError("Memory name is required.")
        if name in {".", ".."} or not MEMORY_NAME_PATTERN.match(name):
            raise ValueError(f"Unsupported memory name: {name}")

    @staticmethod
    def _validate_memory_type(memory_type: str) -> None:
        if memory_type not in MemoryType.ALL:
            raise ValueError(f"Unsupported memory type: {memory_type}")


def load_memory_index(project_path: str | Path) -> tuple[str, list[MemoryIndexEntry]]:
    store = MemoryStore(project_path)
    return store.read_index_text(), store.list_memories()


def load_relevant_memories(
    project_path: str | Path,
    *,
    task_description: str,
    messages: list[Any],
    llm_client: Any | None = None,
    max_memories: int = MAX_RELEVANT_MEMORIES,
) -> tuple[list[MemoryItem], str | None]:
    store = MemoryStore(project_path)
    entries = store.list_memories()
    if not entries or max_memories <= 0:
        return [], None

    selected_names: list[str] = []
    selection_error: str | None = None
    if llm_client is not None:
        try:
            response = llm_client.generate(
                build_memory_selection_prompt(task_description, messages, entries)
            )
            selected_names = _parse_selected_memory_names(response)
        except ValueError as exc:
            selection_error = f"Memory selection parse failed: {type(exc).__name__}: {exc}"
        except Exception as exc:  # pragma: no cover - defensive boundary
            selection_error = f"Memory selection LLM failed: {type(exc).__name__}: {exc}"

    if not selected_names:
        selected = store.search_memories(
            _recent_message_text(task_description, messages),
            limit=max_memories,
        )
        return selected, selection_error

    memories: list[MemoryItem] = []
    known_names = {entry.name for entry in entries}
    for name in selected_names:
        if name not in known_names:
            continue
        try:
            memories.append(store.load_memory(name))
        except (FileNotFoundError, PermissionError, ValueError) as exc:
            selection_error = selection_error or f"{type(exc).__name__}: {exc}"
        if len(memories) >= max_memories:
            break
    return memories, selection_error


def extract_memories(
    project_path: str | Path,
    *,
    task_description: str,
    messages: list[Any],
    tool_results: list[ToolResult],
    answer: str | None,
    llm_client: Any,
    max_existing_entries: int = 50,
) -> tuple[list[MemoryItem], str | None]:
    store = MemoryStore(project_path)
    entries = store.list_memories()
    try:
        response = llm_client.generate(
            build_memory_extraction_prompt(
                task_description=task_description,
                messages=messages,
                tool_results=tool_results,
                answer=answer,
                entries=entries[:max_existing_entries],
            )
        )
        proposals = _parse_extracted_memory_items(response)
    except Exception as exc:
        return [], f"{type(exc).__name__}: {exc}"

    created: list[MemoryItem] = []
    existing_fingerprints = _memory_fingerprints(store)
    for proposal in proposals:
        try:
            memory_type = str(proposal["type"])
            name = str(proposal["name"])
            description = str(proposal.get("description") or "")
            body = str(proposal["body"])
            tags = [str(tag) for tag in proposal.get("tags", [])]
            fingerprint = _fingerprint(memory_type, description, body)
            if fingerprint in existing_fingerprints:
                continue
            item = store.add_memory(
                name=name,
                memory_type=memory_type,
                description=description,
                body=body,
                tags=tags,
                source="auto",
            )
            existing_fingerprints.add(fingerprint)
            created.append(item)
        except (KeyError, TypeError, ValueError, PermissionError):
            continue
    return created, None


def build_memory_selection_prompt(
    task_description: str,
    messages: list[Any],
    entries: list[MemoryIndexEntry],
) -> str:
    catalog = [
        {
            "name": entry.name,
            "type": entry.type,
            "description": entry.description,
            "tags": entry.tags,
        }
        for entry in entries
    ]
    return "\n\n".join(
        [
            "Select project memories relevant to the current Agent task.",
            "Return only a JSON array of memory names. Return [] if none are relevant.",
            f"Task: {task_description}",
            "Recent messages:",
            _recent_message_text(task_description, messages),
            "Memory catalog:",
            json.dumps(catalog, ensure_ascii=False, indent=2),
        ]
    )


def build_memory_extraction_prompt(
    *,
    task_description: str,
    messages: list[Any],
    tool_results: list[ToolResult],
    answer: str | None,
    entries: list[MemoryIndexEntry],
) -> str:
    existing = [
        {
            "name": entry.name,
            "type": entry.type,
            "description": entry.description,
            "tags": entry.tags,
        }
        for entry in entries
    ]
    tool_summary = [
        {
            "tool": result.tool,
            "ok": result.ok,
            "summary": result.summary,
            "error": result.error,
        }
        for result in tool_results
    ]
    return "\n\n".join(
        [
            "Extract durable PyCode memories from this completed Agent turn.",
            "Return only a JSON array. Each item must contain name, type, description, body, and optional tags.",
            f"Allowed types: {sorted(MemoryType.ALL)}",
            "Only include genuinely new long-lived knowledge. Do not duplicate existing memories.",
            "Use type=user for user preferences, feedback for collaboration rules, project for project facts, reference for pointers or commands.",
            f"Task: {task_description}",
            "Recent messages:",
            _recent_message_text(task_description, messages, limit=10),
            "Tool result summaries:",
            json.dumps(tool_summary, ensure_ascii=False, indent=2),
            f"Final answer: {answer or 'N/A'}",
            "Existing memory index:",
            json.dumps(existing, ensure_ascii=False, indent=2),
        ]
    )


def format_relevant_memories(memories: list[MemoryItem]) -> str:
    if not memories:
        return ""
    parts = ["<relevant_memories>"]
    for memory in memories:
        parts.append(
            "\n".join(
                [
                    f"## {memory.name}",
                    f"type: {memory.type}",
                    f"description: {memory.description}",
                    f"path: {memory.path}",
                    memory.body,
                ]
            )
        )
    parts.append("</relevant_memories>")
    return "\n\n".join(parts)


def _format_memory_file(item: MemoryItem) -> str:
    metadata = {
        "name": item.name,
        "type": item.type,
        "description": item.description,
        "tags": item.tags,
        "source": item.source,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }
    lines = ["---"]
    for key, value in metadata.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.extend(["---", "", item.body.strip(), ""])
    return "\n".join(lines)


def _parse_memory_file(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        raise ValueError("Memory file is missing frontmatter.")
    parts = text.split("---", 2)
    if len(parts) < 3:
        raise ValueError("Memory file has invalid frontmatter.")
    metadata: dict[str, Any] = {}
    for line in parts[1].splitlines():
        stripped = line.strip()
        if not stripped or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        value = value.strip()
        try:
            metadata[key.strip()] = json.loads(value)
        except json.JSONDecodeError:
            metadata[key.strip()] = value.strip("\"'")
    return metadata, parts[2]


def _parse_selected_memory_names(response: str) -> list[str]:
    data = parse_json_array_response(response, allow_empty=True)
    return [str(item) for item in data if isinstance(item, str)]


def _parse_extracted_memory_items(response: str) -> list[dict[str, Any]]:
    data = parse_json_array_response(response, allow_empty=True)
    return [item for item in data if isinstance(item, dict)]


def _recent_message_text(
    task_description: str,
    messages: list[Any],
    *,
    limit: int = 3,
) -> str:
    parts = [task_description]
    for message in messages[-limit:]:
        role = getattr(message, "role", "message")
        content = getattr(message, "content", str(message))
        parts.append(f"{role}: {content}")
    return "\n".join(parts)


def _query_terms(query: str) -> list[str]:
    terms: list[str] = []
    for token in re.findall(r"[\w\u4e00-\u9fff]{2,}", query):
        lowered = token.lower().strip()
        if len(lowered) < 2:
            continue
        terms.append(lowered)
        if re.search(r"[\u4e00-\u9fff]", lowered) and len(lowered) > 2:
            terms.extend(lowered[index : index + 2] for index in range(len(lowered) - 1))
    return dedupe_preserve_order(terms)


def _memory_fingerprints(store: MemoryStore) -> set[str]:
    fingerprints: set[str] = set()
    for entry in store.list_memories():
        try:
            item = store.load_memory(entry.name)
        except (FileNotFoundError, PermissionError, ValueError):
            continue
        fingerprints.add(_fingerprint(item.type, item.description, item.body))
    return fingerprints


def _fingerprint(memory_type: str, description: str, body: str) -> str:
    normalized = " ".join(f"{memory_type} {description} {body}".lower().split())
    return normalized[:500]
