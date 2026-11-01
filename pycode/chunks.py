"""Deterministic lexical chunks, sharing the Parser's AST and source text.

Chunks form a non-overlapping line partition of each file. Nested definitions
own their own lines; parent parts retain lexical identity and full symbol bounds.
Parts are evidence slices, not independently executable Python programs.
"""
from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
from uuid import UUID

from pycode.models import ChunkSpan, ProjectIndex
from pycode.utils import normalize_path


CHUNK_SCHEMA_VERSION = 1
MAX_CHUNK_LINES = 120


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _lines(source: str) -> list[str]:
    # AST line numbers count LF, not Unicode separators embedded in literals.
    lines = source.split("\n")
    return [line + "\n" for line in lines[:-1]] + ([lines[-1]] if lines[-1] else [])


def _relative_path(path: str) -> str:
    normalized = normalize_path(path)
    if (not normalized or PurePosixPath(normalized).is_absolute() or ":" in normalized
            or any(part in {"", ".", ".."} for part in normalized.split("/"))):
        raise ValueError("Chunk path must be repository-relative.")
    return normalized


@dataclass(frozen=True)
class SnapshotBinding:
    project_id: str
    snapshot_id: str
    commit_sha: str

    def __post_init__(self):
        for value in (self.project_id, self.snapshot_id):
            if str(UUID(value)) != value:
                raise ValueError("Chunk binding requires canonical UUIDs.")
        if not isinstance(self.commit_sha, str) or not self.commit_sha.strip():
            raise ValueError("Chunk binding requires an explicit Commit.")


@dataclass(frozen=True)
class CodeChunk:
    id: str
    project_id: str
    snapshot_id: str
    commit_sha: str
    path: str
    language: str
    span: ChunkSpan
    content_hash: str
    content: str


@dataclass(frozen=True)
class ChunkFile:
    path: str
    content_hash: str
    line_count: int


@dataclass(frozen=True)
class ChunkIndex:
    schema_version: int
    binding: SnapshotBinding
    files: list[ChunkFile]
    chunks: list[CodeChunk]


def _chunk_id(binding: SnapshotBinding, path: str, span: ChunkSpan, digest: str) -> str:
    identity = {"schema_version": CHUNK_SCHEMA_VERSION, "binding": asdict(binding),
                "path": path, "language": "python", "span": asdict(span), "content_hash": digest}
    return _hash(json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":")))


def collect_chunk_spans(tree: ast.Module, source: str, *, max_lines: int = MAX_CHUNK_LINES) -> list[ChunkSpan]:
    """Use an already parsed AST; never scan, read or parse a file here."""
    if max_lines < 1:
        raise ValueError("Chunk line budget must be positive.")
    lines = _lines(source)
    if not lines:
        return []
    scopes = [{"symbol_name": "<module>", "qualified_name": "<module>",
               "symbol_key": "<module>", "symbol_kind": "module", "class_name": None,
               "parent_symbol": None, "occurrence": 1,
               "symbol_start_line": 1, "symbol_end_line": len(lines), "node": tree}]
    owners = [0] * len(lines)
    occurrences: dict[tuple[str, str], int] = defaultdict(int)

    def visit(node, parent):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            owner = scopes[parent]
            key = (owner["symbol_key"], node.name)
            occurrences[key] += 1
            occurrence = occurrences[key]
            qualified = node.name if parent == 0 else owner["qualified_name"] + "." + node.name
            kind = ("class" if isinstance(node, ast.ClassDef) else
                    "method" if owner["symbol_kind"] == "class" else "function")
            start = min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
            scope = {"symbol_name": node.name, "qualified_name": qualified,
                     "symbol_key": f"{owner['symbol_key']}/{node.name}[{occurrence}]",
                     "symbol_kind": kind,
                     "class_name": owner["qualified_name"] if owner["symbol_kind"] == "class" else owner["class_name"],
                     "parent_symbol": owner["symbol_key"], "occurrence": occurrence,
                     "symbol_start_line": start, "symbol_end_line": node.end_lineno, "node": node}
            parent = len(scopes)
            scopes.append(scope)
            owners[start - 1:node.end_lineno] = [parent] * (node.end_lineno - start + 1)
        for child in ast.iter_child_nodes(node):
            visit(child, parent)

    visit(tree, 0)
    segments: dict[int, list[tuple[int, int]]] = defaultdict(list)
    start = 1
    for index in range(1, len(lines) + 1):
        if index == len(lines) or owners[index] != owners[start - 1]:
            segments[owners[start - 1]].append((start, index))
            start = index + 1
    result = []
    for owner, ranges in segments.items():
        scope = scopes[owner]
        # Only immediate statements are safe split points: never split a string,
        # expression or compound statement merely to meet the line budget.
        boundaries = {statement.end_lineno for statement in scope["node"].body}
        parts = []
        for start, end in ranges:
            while start <= end:
                allowed = sorted(line for line in boundaries | {end} if start <= line <= end)
                fitting = [line for line in allowed if line < start + max_lines]
                stop = max(fitting) if fitting else min(allowed)
                parts.append((start, stop))
                start = stop + 1
        metadata = {key: value for key, value in scope.items() if key != "node"}
        for part, (start, end) in enumerate(parts, 1):
            result.append(ChunkSpan(**metadata, start_line=start, end_line=end,
                                    part_index=part, part_count=len(parts),
                                    oversized=end - start + 1 > max_lines))
    return sorted(result, key=lambda span: span.start_line)


def build_chunk_index(index: ProjectIndex, binding: SnapshotBinding) -> ChunkIndex:
    files, chunks = [], []
    for file in sorted(index.files, key=lambda file: normalize_path(file.path)):
        if file.source_text is None:
            raise ValueError("Chunks require the existing fresh parse result, not a legacy Index reload.")
        path, lines = _relative_path(file.path), _lines(file.source_text)
        files.append(ChunkFile(path, _hash(file.source_text), len(lines)))
        for span in file.chunk_spans:
            content = "".join(lines[span.start_line - 1:span.end_line])
            digest = _hash(content)
            chunks.append(CodeChunk(_chunk_id(binding, path, span, digest),
                                    binding.project_id, binding.snapshot_id, binding.commit_sha,
                                    path, "python", span, digest, content))
    result = ChunkIndex(CHUNK_SCHEMA_VERSION, binding, files, chunks)
    validate_chunk_index(result, binding)
    return result


def validate_chunk_index(index: ChunkIndex, binding: SnapshotBinding) -> None:
    if type(index.schema_version) is not int or index.schema_version != CHUNK_SCHEMA_VERSION or index.binding != binding:
        raise ValueError("Chunk schema or Snapshot binding mismatch.")
    files = {file.path: file for file in index.files}
    if len(files) != len(index.files):
        raise ValueError("Duplicate Chunk file.")
    seen = set()
    by_file: dict[str, list[CodeChunk]] = defaultdict(list)
    for chunk in index.chunks:
        span = chunk.span
        if (chunk.path not in files or _relative_path(chunk.path) != chunk.path
                or chunk.language != "python"
                or (chunk.project_id, chunk.snapshot_id, chunk.commit_sha) !=
                (binding.project_id, binding.snapshot_id, binding.commit_sha)):
            raise ValueError("Chunk identity mismatch.")
        integers = (span.start_line, span.end_line, span.symbol_start_line, span.symbol_end_line,
                    span.occurrence, span.part_index, span.part_count)
        if (any(type(value) is not int or value < 1 for value in integers)
                or not span.symbol_start_line <= span.start_line <= span.end_line <= span.symbol_end_line
                or span.part_index > span.part_count or type(span.oversized) is not bool
                or span.symbol_kind not in {"module", "class", "function", "method"}
                or not all(isinstance(value, str) and value for value in
                           (span.symbol_name, span.qualified_name, span.symbol_key))):
            raise ValueError("Invalid Chunk bounds or symbol metadata.")
        if (chunk.content_hash != _hash(chunk.content)
                or len(_lines(chunk.content)) != span.end_line - span.start_line + 1
                or chunk.id != _chunk_id(binding, chunk.path, span, chunk.content_hash)
                or chunk.id in seen):
            raise ValueError("Invalid or duplicate Chunk content/ID.")
        seen.add(chunk.id)
        by_file[chunk.path].append(chunk)
    for path, file in files.items():
        if _relative_path(path) != path or type(file.line_count) is not int or file.line_count < 0:
            raise ValueError("Invalid Chunk file metadata.")
        position, content = 1, []
        for chunk in sorted(by_file[path], key=lambda chunk: chunk.span.start_line):
            if chunk.span.start_line != position:
                raise ValueError("Chunk file has gaps or overlapping lines.")
            position = chunk.span.end_line + 1
            content.append(chunk.content)
        if position != file.line_count + 1 or _hash("".join(content)) != file.content_hash:
            raise ValueError("Chunks do not reconstruct the complete source file.")


def save_chunk_index(index: ChunkIndex, path: Path) -> None:
    validate_chunk_index(index, index.binding)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(index), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_chunk_index(path: Path, binding: SnapshotBinding) -> ChunkIndex:
    data = json.loads(path.read_text(encoding="utf-8"))
    result = ChunkIndex(data["schema_version"], SnapshotBinding(**data["binding"]),
                        [ChunkFile(**file) for file in data["files"]],
                        [CodeChunk(**{**chunk, "span": ChunkSpan(**chunk["span"])}) for chunk in data["chunks"]])
    validate_chunk_index(result, binding)
    return result
