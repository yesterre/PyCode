from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any

from pycode.agent._time_utils import format_timestamp, utc_now
from pycode.agent import memory_policy as policy
from pycode.agent.memory_record import (
    MAX_DIAGNOSTICS, MAX_RECORD_BYTES, MAX_RECORDS, MAX_TOTAL_BYTES,
    SCHEMA_VERSION, MemoryScope, atomic_write, check_path, digest,
    effective_scope, execution_check, provenance_for, read_bounded,
    record_identity, validate_code_evidence, validate_provenance, write_lock,
)
from pycode.constants import DEFAULT_MEMORY_DIR
from pycode.tools.base import ToolResult
from pycode.utils import dedupe_preserve_order, parse_json_array_response


MEMORY_INDEX_FILE = "MEMORY.md"
MEMORY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_RELEVANT_MEMORIES = 5


class MemoryType(StrEnum):
    PROJECT = "project"
    WORKFLOW = "workflow"
    ANALYSIS = "analysis"
    PREFERENCE = "preference"
    LIMITATION = "limitation"


MemoryType.ALL = {
    MemoryType.PROJECT,
    MemoryType.WORKFLOW,
    MemoryType.ANALYSIS,
    MemoryType.PREFERENCE,
    MemoryType.LIMITATION,
}

MEMORY_TYPE_ORDER = [
    MemoryType.PROJECT,
    MemoryType.WORKFLOW,
    MemoryType.ANALYSIS,
    MemoryType.PREFERENCE,
    MemoryType.LIMITATION,
]


@dataclass
class MemoryIndexEntry:
    id: str
    type: str
    title: str = ""
    summary: str = ""
    path: str = ""
    tags: list[str] = field(default_factory=list)
    confidence: float = 1.0
    related_files: list[str] = field(default_factory=list)
    schema_version: int = 1
    provenance: dict[str, Any] | None = None
    record_id: str | None = None
    lifecycle: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "summary": self.summary,
            "path": self.path,
            "tags": list(self.tags),
            "confidence": self.confidence,
            "related_files": list(self.related_files),
            "schema_version": self.schema_version,
            "provenance": self.provenance,
            "record_id": self.record_id,
            "lifecycle": self.lifecycle,
        }


@dataclass
class MemoryItem:
    id: str
    type: str
    title: str
    summary: str
    body: str
    tags: list[str] = field(default_factory=list)
    source: str = "manual"
    confidence: float = 1.0
    related_files: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: format_timestamp(utc_now()))
    updated_at: str = field(default_factory=lambda: format_timestamp(utc_now()))
    path: str = ""
    schema_version: int = 1
    provenance: dict[str, Any] | None = None
    record_id: str | None = None
    lifecycle: dict[str, Any] | None = None

    def to_index_entry(self) -> MemoryIndexEntry:
        return MemoryIndexEntry(
            id=self.id,
            type=self.type,
            title=self.title,
            summary=self.summary,
            path=self.path or f"{self.id}.md",
            tags=list(self.tags),
            confidence=self.confidence,
            related_files=list(self.related_files),
            schema_version=self.schema_version,
            provenance=self.provenance,
            record_id=self.record_id,
            lifecycle=self.lifecycle,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "title": self.title,
            "summary": self.summary,
            "body": self.body,
            "tags": list(self.tags),
            "source": self.source,
            "confidence": self.confidence,
            "related_files": list(self.related_files),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "path": self.path,
            "schema_version": self.schema_version,
            "provenance": self.provenance,
            "record_id": self.record_id,
            "lifecycle": self.lifecycle,
        }


@dataclass
class MemoryRunInfo:
    index_entries: list[MemoryIndexEntry] = field(default_factory=list)
    relevant_memories: list[MemoryItem] = field(default_factory=list)
    extracted_memories: list[MemoryItem] = field(default_factory=list)
    selection_error: str | None = None
    extraction_error: str | None = None
    reason_counts: dict[str, int] = field(default_factory=dict)
    selection_reasons: dict[str, str] = field(default_factory=dict)
    selection_attempted: bool = False
    extraction_attempted: bool = False

    def public_summary(self):
        return dict(schema_version=1,selected_count=len(self.relevant_memories),
            created_count=len(self.extracted_memories),reason_counts=dict(self.reason_counts),
            source_counts=dict(Counter(m.provenance['origin'] for m in self.relevant_memories if m.provenance)),
            references=[dict(record_id=m.record_id,origin=m.provenance['origin'],
                reason=self.selection_reasons.get(m.id,'keyword_fallback'))
                for m in self.relevant_memories[:5] if m.record_id and m.provenance])

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
        *,
        scope: MemoryScope | None = None,
        origin: str = "local_unknown",
        evidence: tuple[dict, ...] = (),
        retrieval_context: Any = None,
        clock=None,
    ) -> None:
        self.project_path = Path(project_path).resolve()
        if memory_dir is None:
            self.memory_dir = self.project_path / DEFAULT_MEMORY_DIR
        else:
            candidate = Path(memory_dir)
            if candidate.is_absolute():
                self.memory_dir = candidate.absolute()
            else:
                self.memory_dir = (self.project_path / candidate).absolute()
        self._ensure_memory_dir_in_project()
        self.scope = effective_scope(self.project_path, scope)
        self.origin, self.evidence = origin, evidence
        self.retrieval_context = retrieval_context
        self.diagnostics: list[str] = []
        self.reason_counts = Counter()
        self.last_write = 'created'
        self._now = clock or utc_now

    def _check(self):
        execution_check()
        if effective_scope(self.project_path, self.scope) != self.scope:
            raise PermissionError("Memory scope changed")
        check_path(self.memory_dir, self.project_path)

    def _diagnose(self, code):
        if code in policy.PUBLIC_REASONS:
            self.reason_counts[code] += 1
        if code not in self.diagnostics and len(self.diagnostics) < MAX_DIAGNOSTICS:
            self.diagnostics.append(code)

    def _applicable(self, item):
        return policy.scope_rejection(item,self.scope) is None

    @property
    def index_path(self) -> Path:
        return self.memory_dir / MEMORY_INDEX_FILE

    def add_memory(
        self,
        *,
        memory_id: str,
        memory_type: str,
        title: str,
        summary: str,
        body: str,
        tags: list[str] | None = None,
        source: str = "manual",
        confidence: float = 1.0,
        related_files: list[str] | None = None,
        allow_existing: bool = False,
        _state: str = 'active',
    ) -> MemoryItem:
        self._check()
        with write_lock(self.memory_dir, self.project_path):
            return self._add_memory(memory_id=memory_id, memory_type=memory_type,
                title=title, summary=summary, body=body, tags=tags, source=source,
                confidence=confidence, related_files=related_files, allow_existing=allow_existing,
                _state=_state)

    def _add_memory(self, *, memory_id, memory_type, title, summary, body, tags,
                    source, confidence, related_files, allow_existing, _state):
        if not memory_id:
            raise ValueError("Memory id is required.")
        if not body:
            raise ValueError("Memory body is required.")
        self._validate_memory_type(memory_type)
        # Check before id normalization, which would erase credential syntax
        # while leaving its value in a filename (e.g. token=... -> token-...).
        if policy.sensitive('\n'.join([memory_id,title,summary,body,
                                      *[str(v) for v in tags or []],
                                      *[str(v) for v in related_files or []]])):
            raise ValueError('memory_sensitive_content')

        actual_id = self._slugify(memory_id)
        existing = self._memory_path(actual_id)
        created_at = format_timestamp(utc_now())
        old = None
        if allow_existing and existing.exists():
            from pycode.agent.budget import current_budget
            if current_budget() is not None:
                raise PermissionError('Agent cannot revise or delete project memories')
            old = self._load_memory(existing)
            if (old.schema_version != SCHEMA_VERSION or not self._applicable(old)
                    or old.provenance['origin'] != self._new_provenance(source)['origin']):
                raise ValueError("Cannot implicitly upgrade or rebind Memory")

        actual_id = self._unique_name(actual_id)

        item = MemoryItem(
            id=actual_id,
            type=memory_type,
            title=title or actual_id,
            summary=summary,
            body=body,
            tags=[str(tag) for tag in tags or []],
            source=source,
            confidence=_normalize_confidence(confidence),
            related_files=[str(path) for path in related_files or []],
            created_at=created_at,
            updated_at=format_timestamp(utc_now()),
            path=f"{actual_id}.md",
            schema_version=SCHEMA_VERSION,
            provenance=self._new_provenance(source),
        )
        item.record_id = record_identity(item.id, item.provenance)
        item.lifecycle = policy.lifecycle_for(item,self._now())
        if old is not None and policy.namespace(old)!=policy.namespace(item):
            raise ValueError('Cannot implicitly upgrade or rebind Memory')
        policy.validate_content(item)
        raw = self._scan_items()
        peers = [m for m in raw if m.lifecycle and policy.namespace(m)==policy.namespace(item)]
        active = policy.eligible(peers,self.scope,self._now())
        parent_ids={parent for m in peers for parent in m.lifecycle['parents']}
        heads=[m for m in peers if m.record_id not in parent_ids]
        if old is None:
            duplicate = next((m for m in active if policy.content_key(m)==policy.content_key(item)),None)
            if duplicate is not None:
                self.last_write='duplicate'
                self._diagnose('duplicate')
                return duplicate
            conflicts=[m for m in heads if m.lifecycle['target']==item.lifecycle['target']
                       and m.lifecycle['state'] in {'active','conflict'}]
            if conflicts:
                item.lifecycle.update(state='conflict',reason='conflicting_content',
                    parents=[m.record_id for m in conflicts[:8]],
                    revision=max(m.lifecycle['revision'] for m in conflicts)+1)
                self._diagnose('conflict')
            elif any(policy.near_duplicate(item,m) for m in active[:policy.MAX_NEAR_CHECKS]):
                self._diagnose('near_duplicate_candidate')
        else:
            if _state not in {'active','invalidated','deleted'}:
                raise ValueError('Invalid explicit Memory action')
            item.lifecycle.update(target=old.lifecycle['target'],state=_state,
                reason={'active':'explicit_revision','invalidated':'explicit_invalidation','deleted':'explicit_delete'}[_state],
                parents=list(dict.fromkeys([old.record_id,*[m.record_id for m in heads
                    if m.lifecycle['target']==old.lifecycle['target']]])),
                revision=max([old.lifecycle['revision'],*[m.lifecycle['revision'] for m in heads
                    if m.lifecycle['target']==old.lifecycle['target']]])+1)
        policy.validate_lifecycle(item.lifecycle)
        self.last_write=item.lifecycle['state']
        return self._publish_memory(item)

    def _publish_memory(self,item):
        self._save_memory(item)
        try:
            # Persist the complete index, never a view filtered to one Snapshot.
            self._write_index(self._list_memories(applicable_only=False))
        except (OSError, ValueError, UnicodeError):
            self._diagnose('index_write_failed_body_committed')
        return item

    def _new_provenance(self, source):
        from pycode.agent.budget import current_budget
        budget = current_budget()
        origin = 'model_inference' if source == 'auto' else self.origin
        if budget is not None and origin in {'local_unknown', 'user_explicit'}:
            if origin == 'user_explicit':
                raise PermissionError('Agent cannot assert explicit user authority')
            origin = 'model_inference'
        value = provenance_for(self.scope, origin, self.evidence)
        if origin == 'code_derived':
            validate_code_evidence(self.scope, self.evidence, self.retrieval_context)
        return value

    def list_memories(self) -> list[MemoryIndexEntry]:
        return self._list_memories(applicable_only=True)

    def _list_memories(self, *, applicable_only):
        items=self._scan_items()
        if applicable_only:
            counts=Counter()
            items=policy.eligible(items,self.scope,self._now(),counts)
            for reason,count in counts.items():
                self.reason_counts[reason]=max(self.reason_counts[reason],count)
                if reason not in self.diagnostics:self.diagnostics.append(reason)
        return sorted((m.to_index_entry() for m in items),key=lambda e:(e.type,e.id))

    def _scan_items(self):
        self._check()
        if not self.memory_dir.exists():
            return []
        entries, total = [], 0
        paths = sorted(self.memory_dir.glob('*.md'), key=lambda p: (p.name.casefold(), p.name))
        for number, path in enumerate(p for p in paths if p.name.casefold() != MEMORY_INDEX_FILE.casefold()):
            if number >= MAX_RECORDS:
                self._diagnose('record_count_limit')
                break
            try:
                check_path(path, self.project_path)
                size = path.stat().st_size
                if size > MAX_RECORD_BYTES:
                    self._diagnose('record_unreadable')
                    continue  # Invalid files cannot spend valid records' read budget.
                if total + size > MAX_TOTAL_BYTES:
                    self._diagnose('total_capacity_limit')
                    continue
                total += size
                item = self._load_memory(path)
                entries.append(item)
            except (OSError, ValueError, UnicodeError, TypeError, KeyError):
                self._diagnose('record_unreadable')
        return entries

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
            try:
                item = self.load_memory(entry.id)
            except (OSError, ValueError, UnicodeError, TypeError, KeyError):
                self._diagnose('record_unreadable')
                continue
            haystack = " ".join(
                [
                    item.id,
                    item.type,
                    item.title,
                    item.summary,
                    " ".join(item.tags),
                    " ".join(item.related_files),
                    item.body if include_body else "",
                ]
            ).lower()
            score = sum(1 for term in terms if term in haystack)
            if not terms:
                score = 1
            if score > 0:
                scored.append((score, item))
        scored.sort(key=lambda pair: (-pair[0], pair[1].id))
        return [item for _, item in scored[: max(limit, 0)]]

    def load_memory(self, name: str) -> MemoryItem:
        self._check()
        path = self._memory_path(self._slugify(name))
        if not path.exists():
            raise FileNotFoundError(f"Memory does not exist: {name}")
        item = self._load_memory(path)
        if item.lifecycle is None and self.scope is None:
            return item  # Retain the existing local reader; no lifecycle upgrade.
        available=policy.eligible(self._scan_items(),self.scope,self._now())
        if not any(m.record_id==item.record_id for m in available):
            raise PermissionError('Memory is not applicable to this Snapshot')
        return item

    def manage_memory(self, name, operation, *, body=None):
        """Explicit local revision/soft deletion; never exposed to a model Tool."""
        from pycode.agent.budget import current_budget
        if current_budget() is not None:
            raise PermissionError('Agent cannot manage Memory lifecycle')
        self._check()
        if operation not in {'revise','invalidate','delete'}:
            raise ValueError('Invalid Memory action')
        with write_lock(self.memory_dir,self.project_path):
            old=self._load_memory(self._memory_path(self._slugify(name)))
            if not old.lifecycle or not self._applicable(old):
                raise PermissionError('Memory scope mismatch')
            if operation in {'invalidate','delete'}:
                # An explicit local tombstone retracts existing knowledge; it
                # does not assert new facts against an unverified checkout.
                # Preserve the original namespace, clear the producing Run.
                raw=self._scan_items()
                peers=[m for m in raw if m.lifecycle and policy.namespace(m)==policy.namespace(old)
                       and m.lifecycle['target']==old.lifecycle['target']]
                parent_ids={p for m in peers for p in m.lifecycle['parents']}
                parents=list(dict.fromkeys([old.record_id,*[m.record_id for m in peers if m.record_id not in parent_ids]]))
                name=self._unique_name(old.id)
                life=dict(old.lifecycle,state='invalidated' if operation=='invalidate' else 'deleted',
                    expires_at=None,reason='explicit_invalidation' if operation=='invalidate' else 'explicit_delete',
                    parents=parents,revision=max(m.lifecycle['revision'] for m in peers)+1)
                policy.validate_lifecycle(life)
                item=replace(old,id=name,path=name+'.md',title='Explicit local lifecycle action',summary='',
                    body='Memory explicitly retracted by local operator.',source='local_cli',
                    provenance=dict(old.provenance,run_id=None),lifecycle=life,
                    created_at=format_timestamp(self._now()),updated_at=format_timestamp(self._now()))
                item.record_id=record_identity(item.id,item.provenance)
                self.last_write=life['state']
                return self._publish_memory(item)
            return self._add_memory(memory_id=old.id,memory_type=old.type,title=old.title,
                summary=old.summary,body=body if operation=='revise' else 'Memory explicitly '+operation+'d.',
                tags=old.tags,source=old.source,confidence=old.confidence,
                related_files=old.related_files,allow_existing=True,
                _state={'revise':'active','invalidate':'invalidated','delete':'deleted'}[operation])

    def rebuild_index(self) -> list[MemoryIndexEntry]:
        self._check()
        with write_lock(self.memory_dir, self.project_path):
            self._write_index(self._list_memories(applicable_only=False))
        return self.list_memories()

    def _write_index(self, entries: list[MemoryIndexEntry]) -> None:
        atomic_write(self.index_path, self._index_text(entries), self.project_path)

    @staticmethod
    def _index_text(entries):
        sorted_entries = sorted(entries, key=lambda entry: (entry.type, entry.id))
        lines = [
            "# PyCode Memory Index",
            "",
            "This file is generated from `.pclens/memory/*.md`.",
            "",
        ]
        for memory_type in MEMORY_TYPE_ORDER:
            typed_entries = [entry for entry in sorted_entries if entry.type == memory_type]
            if not typed_entries:
                continue
            lines.append(f"## {memory_type}")
            for entry in typed_entries:
                tags = f" tags={','.join(entry.tags)}" if entry.tags else ""
                related = (
                    f" related_files={','.join(entry.related_files)}"
                    if entry.related_files
                    else ""
                )
                lines.append(
                    f"- [{entry.id}]({entry.path}) - {entry.title}: "
                    f"{entry.summary} confidence={entry.confidence}{tags}{related}"
                    f" origin={(entry.provenance or {}).get('origin', 'legacy_unknown')}"
                    f" status={(entry.provenance or {}).get('status', 'unknown')}"
                )
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def read_index_text(self) -> str:
        # A stored index is a cache, never a second authority or unfiltered prompt.
        entries = self.list_memories()
        return self._index_text(entries).strip() if entries else ""

    def _memory_path(self, name: str) -> Path:
        self._validate_memory_name(name)
        path = self.memory_dir / f"{name}.md"
        try:
            path.relative_to(self.memory_dir)
        except ValueError as exc:
            raise PermissionError(f"Memory path escaped memory directory: {name}") from exc
        check_path(path, self.project_path)
        return path

    def _load_memory(self, path: Path) -> MemoryItem:
        try:
            return self._decode_memory(path)
        except RecursionError:
            # Bounded files can still contain excessive JSON/repr nesting.
            # Treat this as one invalid record, never as an execution signal.
            raise ValueError('Memory record nesting exceeds limits') from None

    def _decode_memory(self, path: Path) -> MemoryItem:
        metadata, body = _parse_memory_file(read_bounded(path, self.project_path))
        version = metadata.get('schema_version', 1)
        if type(version) is not int or version not in (1, 2, SCHEMA_VERSION):
            raise ValueError('Unsupported Memory schema version')
        if version == 1:
            # Escaped lone surrogates are not valid logical UTF-8 text, even
            # when the physical legacy file contains only ASCII bytes.
            json.dumps(metadata, ensure_ascii=False).encode('utf-8')
        if version >= 2:
            fields = {'id', 'type', 'title', 'summary', 'tags', 'source', 'confidence',
                                  'related_files', 'created_at', 'updated_at', 'schema_version',
                                  'provenance', 'record_id', 'record_hash'}
            if version==SCHEMA_VERSION:fields.add('lifecycle')
            if set(metadata) != fields:
                raise ValueError('Invalid Memory record fields')
            validate_provenance(metadata.get('provenance'))
            if metadata.get('record_id') != record_identity(metadata.get('id'), metadata['provenance']):
                raise ValueError('Invalid Memory identity')
            expected = metadata.pop('record_hash', None)
            if expected != digest(dict(metadata=metadata, body=body)):
                raise ValueError('Memory integrity check failed')
            _validate_record_fields(metadata)
            if version==SCHEMA_VERSION:
                policy.validate_lifecycle(metadata['lifecycle'])
        memory_type = str(metadata.get("type", ""))
        self._validate_memory_type(memory_type)
        memory_id = str(metadata.get("id") or path.stem)
        self._validate_memory_name(memory_id)
        if memory_id != path.stem:
            raise ValueError('Memory id and filename differ')
        item = MemoryItem(
            id=memory_id,
            type=memory_type,
            title=str(metadata.get("title") or ""),
            summary=str(metadata.get("summary") or ""),
            body=body if version >= 2 else body.strip(),
            tags=[str(tag) for tag in metadata.get("tags", [])],
            source=str(metadata.get("source") or "legacy_unknown"),
            confidence=_normalize_confidence(metadata.get("confidence", 1.0)),
            related_files=[str(item) for item in metadata.get("related_files", [])],
            created_at=str(metadata.get("created_at") or ""),
            updated_at=str(metadata.get("updated_at") or ""),
            path=path.name,
            schema_version=version,
            provenance=metadata.get('provenance') if version >= 2 else None,
            record_id=metadata.get('record_id') if version >= 2 else None,
            lifecycle=metadata.get('lifecycle') if version==SCHEMA_VERSION else None,
        )
        if item.lifecycle:
            expected=policy.lifecycle_for(item,self._now())
            if any(item.lifecycle[k]!=expected[k] for k in ('category','scope')):
                raise ValueError('Memory authority/lifecycle mismatch')
        return item

    def _save_memory(self, item: MemoryItem) -> None:
        self._validate_memory_name(item.id)
        self._validate_memory_type(item.type)
        policy.validate_content(item)
        policy.validate_lifecycle(item.lifecycle)
        text = _format_memory_file(item)
        _validate_record_fields(item.to_dict())
        size = len(text.encode('utf-8'))
        if size > MAX_RECORD_BYTES:
            raise ValueError('Memory record capacity exceeded')
        paths = [p for p in self.memory_dir.glob('*.md') if p.name.casefold() != MEMORY_INDEX_FILE.casefold()]
        if len(paths) + int(not self._memory_path(item.id).exists()) > MAX_RECORDS:
            raise ValueError('Memory record count exceeded')
        total = size
        for path in paths:
            check_path(path, self.project_path)
            if path.name != item.path:
                total += path.stat().st_size
        if total > MAX_TOTAL_BYTES:
            raise ValueError('Memory total capacity exceeded')
        atomic_write(self._memory_path(item.id), text, self.project_path)

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
        if '..' in self.memory_dir.parts:
            raise PermissionError('Memory directory traversal rejected')
        check_path(self.memory_dir, self.project_path)

    @staticmethod
    def _slugify(value: str) -> str:
        text = value.strip().lower()
        text = re.sub(r"[^a-z0-9_.-]+", "-", text)
        text = re.sub(r"-+", "-", text).strip("-._")
        if not text:
            raise ValueError(f"Unsupported memory id: {value}")
        MemoryStore._validate_memory_name(text)
        return text

    @staticmethod
    def _validate_memory_name(name: str) -> None:
        if not name:
            raise ValueError("Memory id is required.")
        base = name.casefold().split('.')[0]
        if (len(name) > 128 or name.endswith('.') or name in {".", ".."}
                or not MEMORY_NAME_PATTERN.fullmatch(name) or base in {'memory', 'con', 'prn', 'aux', 'nul'}
                or re.fullmatch(r'(com|lpt)[0-9]', base)):
            raise ValueError(f"Unsupported memory id: {name}")

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
    trace_recorder: Any | None = None,
    info: MemoryRunInfo | None = None,
) -> tuple[list[MemoryItem], str | None]:
    try:
        store = MemoryStore(project_path)
        entries = store.list_memories()
    except (OSError, ValueError, UnicodeError, TypeError, KeyError):
        return [], 'memory_storage_unavailable'
    if not entries or max_memories <= 0:
        if info is not None:info.reason_counts.update(store.reason_counts)
        return [], None

    ranked=store.search_memories(_recent_message_text(task_description,messages),
                                limit=policy.MAX_SELECTION_CANDIDATES)
    order={item.id:n for n,item in enumerate(ranked)}
    entries=sorted(entries,key=lambda e:(order.get(e.id,len(order)),e.id))
    if len(entries)>policy.MAX_SELECTION_CANDIDATES:store._diagnose('candidate_limit')
    entries=entries[:policy.MAX_SELECTION_CANDIDATES]
    max_memories=min(max_memories,MAX_RELEVANT_MEMORIES)

    selected_names: list[str] = []
    selection_error: str | None = None
    if llm_client is not None:
        if info is not None:info.selection_attempted=True
        if trace_recorder is not None:
            trace_recorder.start_llm("memory_selection")
        try:
            response = llm_client.generate(
                build_memory_selection_prompt(task_description, messages, entries)
            )
            selected_names = _parse_selected_memory_names(response)
            if trace_recorder is not None:
                trace_recorder.finish_llm(
                    "memory_selection", success=True,
                    fallback_used=not bool(selected_names),
                )
        except ValueError as exc:
            if trace_recorder is not None:
                trace_recorder.finish_llm(
                    "memory_selection", success=False, fallback_used=True,
                    error=exc, error_category="schema_validation",
                    error_code="schema_parse_failed",
                )
            selection_error = f"Memory selection parse failed: {type(exc).__name__}"
        except Exception as exc:  # pragma: no cover - defensive boundary
            if trace_recorder is not None:
                trace_recorder.finish_llm(
                    "memory_selection", success=False, fallback_used=True,
                    error=exc,
                )
            selection_error = f"Memory selection LLM failed: {type(exc).__name__}"

    if not selected_names:
        selected = store.search_memories(
            _recent_message_text(task_description, messages),
            limit=max_memories,
        )
        if info is not None:
            info.reason_counts.update(store.reason_counts)
            info.selection_reasons.update({m.id:'keyword_fallback' for m in selected})
            info.reason_counts['keyword_fallback']=len(selected)
        return selected, selection_error

    memories: list[MemoryItem] = []
    known_ids = {entry.id for entry in entries}
    for memory_id in selected_names:
        if memory_id not in known_ids:
            continue
        if any(m.id==memory_id for m in memories):
            continue
        try:
            memories.append(store.load_memory(memory_id))
        except (OSError, ValueError, UnicodeError, TypeError, KeyError):
            selection_error = selection_error or 'memory_storage_unavailable'
        if len(memories) >= max_memories:
            break
    if info is not None:
        info.reason_counts.update(store.reason_counts)
        info.selection_reasons.update({m.id:'llm_selection' for m in memories})
        info.reason_counts['llm_selection']=len(memories)
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
    trace_recorder: Any | None = None,
    info: MemoryRunInfo | None = None,
) -> tuple[list[MemoryItem], str | None]:
    def reason(code,count=1):
        if info is not None:info.reason_counts[code]=info.reason_counts.get(code,0)+count
    if blocked:=policy.extraction_gate(task_description,messages,answer):
        reason(blocked)
        return [],None
    try:
        store = MemoryStore(project_path)
        entries = store.list_memories()
    except (OSError, ValueError, UnicodeError, TypeError, KeyError):
        return [], 'memory_storage_unavailable'
    if info is not None:info.extraction_attempted=True
    if trace_recorder is not None:
        trace_recorder.start_llm("memory_extraction")
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
        if len(response)>65536:
            raise ValueError('Memory proposal capacity exceeded')
        proposals = _parse_extracted_memory_items(response)
    except Exception as exc:
        if trace_recorder is not None:
            trace_recorder.finish_llm(
                "memory_extraction", success=False, fallback_used=False,
                error=exc,
                error_category=(
                    "schema_validation" if isinstance(exc, ValueError) else None
                ),
                error_code=(
                    "schema_parse_failed" if isinstance(exc, ValueError) else None
                ),
            )
        reason('model_failed')
        return [], 'memory_extraction_failed'
    if trace_recorder is not None:
        trace_recorder.finish_llm(
            "memory_extraction", success=True, fallback_used=False,
        )

    created: list[MemoryItem] = []
    extraction_error = None
    total_bytes=0
    if len(proposals)>policy.MAX_PROPOSALS:reason('proposal_limit',len(proposals)-policy.MAX_PROPOSALS)
    for proposal in proposals[:policy.MAX_PROPOSALS]:
        try:
            execution_check()
            if len(created)>=policy.MAX_EXTRACTED:
                reason('proposal_limit')
                break
            if proposal.get('purpose')=='working' or proposal.get('temporary') is True:
                reason('temporary')
                continue
            memory_type = str(proposal["type"])
            memory_id = str(proposal["id"])
            title = str(proposal.get("title") or memory_id)
            summary = str(proposal.get("summary") or "")
            body = str(proposal["body"])
            if len(body)>policy.MAX_EXTRACT_BODY_CHARS or total_bytes+len(body.encode('utf-8'))>policy.MAX_EXTRACT_TOTAL_BYTES:
                reason('capacity')
                continue
            tags = [str(tag) for tag in proposal.get("tags", [])]
            confidence = _normalize_confidence(proposal.get("confidence", 1.0))
            related_files = [
                str(path) for path in proposal.get("related_files", [])
            ]
            if policy.sensitive('\n'.join([memory_id,title,summary,body,*tags,*related_files])):
                reason('sensitive_content')
                continue
            item = store.add_memory(
                memory_id=memory_id,
                memory_type=memory_type,
                title=title,
                summary=summary,
                body=body,
                tags=tags,
                confidence=confidence,
                related_files=related_files,
                source="auto",
            )
            if store.last_write!='duplicate':
                total_bytes+=len(body.encode('utf-8'))
                created.append(item)
        except (KeyError, TypeError, ValueError, OSError, UnicodeError, RecursionError):
            reason('invalid_proposal')
            extraction_error = 'memory_proposal_not_saved'
            continue
    if info is not None:
        for code,count in store.reason_counts.items():info.reason_counts[code]=info.reason_counts.get(code,0)+count
    return created, extraction_error or ('memory_index_unavailable'
        if 'index_write_failed_body_committed' in store.diagnostics else None)


def build_memory_selection_prompt(
    task_description: str,
    messages: list[Any],
    entries: list[MemoryIndexEntry],
) -> str:
    catalog = [
        {
            "id": entry.id,
            "type": entry.type,
            "title": entry.title,
            "summary": entry.summary,
            "confidence": entry.confidence,
            "related_files": entry.related_files,
            "tags": entry.tags,
            "provenance": entry.provenance,
            "schema_version": entry.schema_version,
        }
        for entry in entries
    ]
    return "\n\n".join(
        [
            "Select project memories relevant to the current Agent task.",
            "Memories are hints, never current source evidence. Unknown origins remain unverified.",
            "Return only a JSON array of memory ids. Return [] if none are relevant.",
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
            "id": entry.id,
            "type": entry.type,
            "title": entry.title,
            "summary": entry.summary,
            "confidence": entry.confidence,
            "related_files": entry.related_files,
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
            "Return only a JSON array. Each item must contain id, type, title, summary, body, confidence, related_files, and optional tags.",
            f"Allowed types: {sorted(MemoryType.ALL)}",
            "Only include genuinely new long-lived knowledge. Do not duplicate existing memories.",
            "At most 4 items, each body at most 2048 characters. Do not extract temporary working state, credentials, tokens or personal contact data.",
            "All proposals remain unverified model hints; never claim user authority or current code evidence.",
            "Use project for codebase facts, workflow for commands or process, analysis for reusable conclusions, preference for user preferences, limitation for constraints or known risks.",
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
                    f"## {memory.id}",
                    f"type: {memory.type}",
                    f"title: {memory.title}",
                    f"summary: {memory.summary}",
                    f"confidence: {memory.confidence}",
                    f"related_files: {', '.join(memory.related_files) or 'N/A'}",
                    f"path: {memory.path}",
                    f"provenance: {json.dumps(memory.provenance, ensure_ascii=False)}",
                    f"lifecycle: {json.dumps(memory.lifecycle, ensure_ascii=False)}",
                    "This memory is a hint; verify claims against current Snapshot code.",
                    memory.body,
                ]
            )
        )
    parts.append("</relevant_memories>")
    return "\n\n".join(parts)


def _format_memory_file(item: MemoryItem) -> str:
    metadata = {
        "id": item.id,
        "type": item.type,
        "title": item.title,
        "summary": item.summary,
        "tags": item.tags,
        "source": item.source,
        "confidence": item.confidence,
        "related_files": item.related_files,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
        "schema_version": item.schema_version,
        "provenance": item.provenance,
        "record_id": item.record_id,
        "lifecycle": item.lifecycle,
    }
    metadata['record_hash'] = digest(dict(metadata=metadata, body=item.body))
    lines = ["---"]
    for key, value in metadata.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    return "\n".join(lines) + "\n---\n" + item.body


def _parse_memory_file(text: str) -> tuple[dict[str, Any], str]:
    opening = re.match(r'\A---\r?\n', text)
    if opening is None:
        raise ValueError("Memory file is missing frontmatter.")
    closing = re.search(r'^---\r?\n', text[opening.end():], re.MULTILINE)
    if closing is None:
        raise ValueError("Memory file has invalid frontmatter.")
    start = opening.end()
    header = text[start:start + closing.start()]
    body = text[start + closing.end():]
    metadata: dict[str, Any] = {}
    invalid = False
    for line in header.split('\n'):
        stripped = line.strip()
        if not stripped:
            continue
        if ':' not in stripped:
            invalid = True
            continue
        key, value = stripped.split(":", 1)
        key = key.strip()
        invalid = invalid or key in metadata
        value = value.strip()
        try:
            metadata[key] = json.loads(value)
        except json.JSONDecodeError:
            invalid = True
            metadata[key] = value.strip("\"'")
    if metadata.get('schema_version') == SCHEMA_VERSION and invalid:
        raise ValueError('Invalid versioned Memory frontmatter')
    return metadata, body


def _validate_record_fields(value):
    for key, limit in (('id', 128), ('type', 32), ('title', 1024), ('summary', 4096),
                       ('source', 64), ('created_at', 64), ('updated_at', 64)):
        if not isinstance(value.get(key), str) or len(value[key]) > limit:
            raise ValueError('Invalid Memory field')
    for key in ('tags', 'related_files'):
        items = value.get(key)
        if (not isinstance(items, list) or len(items) > 32
                or any(not isinstance(v, str) or len(v) > 1024 for v in items)):
            raise ValueError('Invalid Memory list field')
    confidence = value.get('confidence')
    if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError('Invalid Memory confidence')


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
            item = store.load_memory(entry.id)
        except (OSError, ValueError, UnicodeError, TypeError, KeyError):
            continue
        fingerprints.add(_fingerprint(item.type, item.title, item.summary, item.body))
    return fingerprints


def _fingerprint(memory_type: str, title: str, summary: str, body: str) -> str:
    return digest(dict(type=memory_type,title=title,summary=summary,body=body))


def _normalize_confidence(value: Any) -> float:
    try:
        confidence = float(value)
    except (TypeError, ValueError):
        confidence = 1.0
    return max(0.0, min(1.0, confidence))
