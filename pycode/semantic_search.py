"""Read-only search over the existing v1 semantic artifact. No publication IO."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
import struct

from pycode.chunks import ChunkIndex, SnapshotBinding, validate_chunk_index
from pycode.embedding import prepare_inputs, validate_result
from pycode.models import ChunkSpan
from pycode.semantic_index import SemanticIdentity, read_artifact


class VectorUnavailable(Exception):
    """Public, bounded diagnostic; never includes Provider errors or query text."""

    def __init__(self, code="vector_unavailable"):
        allowed = {"vector_unavailable", "vector_disabled", "vector_configuration",
                   "vector_artifact_unavailable", "vector_provider_failed",
                   "vector_invalid_response", "vector_binding_mismatch", "vector_query_too_long"}
        self.code = code if code in allowed else "vector_unavailable"
        super().__init__(self.code)


@dataclass(frozen=True)
class VectorHit:
    path: str
    span: ChunkSpan
    score: float


@dataclass(frozen=True)
class VectorResult:
    binding: SnapshotBinding
    hits: tuple[VectorHit, ...]


@dataclass(frozen=True)
class ReadOnlyVectorIndex:
    target: SemanticIdentity
    chunks: ChunkIndex = field(repr=False)
    rows: tuple[tuple[str, tuple[float, ...]], ...] = field(repr=False)

    @classmethod
    def load(cls, directory: Path, target: SemanticIdentity, chunks: ChunkIndex):
        validate_chunk_index(chunks, target.binding)
        manifest = read_artifact(directory, target, chunks)
        # Search exactly the bytes whose digest was validated, even if a file
        # changes after read_artifact. Mapping is regenerated from trusted chunks.
        data = (directory / "vectors.f32").read_bytes()
        if hashlib.sha256(data).hexdigest() != manifest["vectors_sha256"]:
            raise ValueError("Vector content changed during read")
        segments = prepare_inputs(chunks, target.max_input_chars)
        rows = []
        width = target.dimension * 4
        for i, segment in enumerate(segments):
            values = struct.unpack('<' + 'f' * target.dimension, data[i * width:(i + 1) * width])
            norm = math.hypot(*values)
            if not math.isfinite(norm) or norm == 0:
                raise ValueError("Invalid cosine document vector")
            rows.append((segment.chunk_id, tuple(v / norm for v in values)))
        return cls(target, chunks, tuple(rows))

    def search(self, response, limit: int) -> VectorResult:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("Invalid vector limit")
        validate_result(response, 1, self.target.provider, self.target.model, self.target.dimension)
        vector = response.vectors[0]
        norm = math.hypot(*vector)
        if norm == 0 or not math.isfinite(norm):
            raise ValueError("Invalid cosine query vector")
        query = tuple(v / norm for v in vector)
        chunks = {chunk.id: chunk for chunk in self.chunks.chunks}
        # Max cosine over all segments/parts of one definition prevents long
        # definitions receiving extra votes or exhausting Top-K with duplicates.
        scores, representatives = {}, {}
        for chunk_id, row in self.rows:
            chunk = chunks[chunk_id]
            span = chunk.span
            key = (chunk.path, span.symbol_key, span.symbol_start_line, span.symbol_end_line)
            score = max(-1.0, min(1.0, math.fsum(a * b for a, b in zip(query, row))))
            scores[key] = max(scores.get(key, -math.inf), score)
            previous = representatives.get(key)
            if previous is None or (span.start_line, span.part_index) < (previous.start_line, previous.part_index):
                representatives[key] = span
        keys = sorted(scores, key=lambda k: (-scores[k], k[0], k[2], k[3], k[1]))[:limit]
        return VectorResult(self.target.binding, tuple(
            VectorHit(key[0], representatives[key], scores[key]) for key in keys))
