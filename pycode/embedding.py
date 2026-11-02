"""Embedding contracts and deterministic inputs; no network or repository scanning."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Protocol, Sequence

from pycode.chunks import ChunkIndex

INPUT_VERSION = 1
SEGMENT_VERSION = 1
INPUT_PREFIX = "Python code:\n"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def identity(value: dict) -> str:
    return digest(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False))


class EmbeddingError(Exception):
    """Only a bounded, public error code crosses the Provider boundary."""

    def __init__(self, code: str = "embedding_failed"):
        allowed = {"embedding_failed", "embedding_unavailable", "embedding_invalid_input",
                   "embedding_invalid_response", "embedding_timeout", "embedding_rate_limited",
                   "embedding_authentication", "embedding_configuration"}
        self.code = code if code in allowed else "embedding_failed"
        super().__init__(self.code)


@dataclass(frozen=True)
class EmbeddingResult:
    provider: str
    model: str
    dimension: int
    input_count: int
    vectors: list[list[float]] = field(repr=False)
    usage: dict[str, int] | None = None
    batch_sizes: tuple[int, ...] = ()


class EmbeddingProvider(Protocol):
    provider: str
    model: str
    dimension: int

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingResult: ...
    def embed_query(self, text: str) -> EmbeddingResult: ...


def validate_result(result: EmbeddingResult, count: int, provider: str,
                    model: str, dimension: int) -> None:
    if (result.provider != provider or result.model != model
            or type(result.dimension) is not int or result.dimension != dimension
            or type(result.input_count) is not int or result.input_count != count
            or len(result.vectors) != count):
        raise EmbeddingError("embedding_invalid_response")
    for vector in result.vectors:
        if len(vector) != dimension or any(
            type(value) not in (float, int) or not math.isfinite(value) for value in vector
        ):
            raise EmbeddingError("embedding_invalid_response")


@dataclass(frozen=True)
class EmbeddingSegment:
    id: str
    chunk_id: str
    segment_index: int
    segment_count: int
    start_offset: int
    end_offset: int
    text_hash: str
    text: str = field(repr=False)

    def metadata(self) -> dict:
        return {key: value for key, value in vars(self).items() if key != "text"}


def prepare_inputs(chunks: ChunkIndex, max_input_chars: int = 1800) -> list[EmbeddingSegment]:
    """Offsets count Unicode code points in unmodified Chunk content (half-open)."""
    if type(max_input_chars) is not int or not len(INPUT_PREFIX) < max_input_chars <= 2000:
        raise EmbeddingError("embedding_configuration")
    budget = max_input_chars - len(INPUT_PREFIX)
    segments = []
    for chunk in chunks.chunks:
        offsets = []
        start = 0
        while start < len(chunk.content):
            end = min(start + budget, len(chunk.content))
            if end < len(chunk.content):
                newline = chunk.content.rfind("\n", start, end)
                if newline >= start:
                    end = newline + 1
            offsets.append((start, end))
            start = end
        if not offsets:
            offsets = [(0, 0)]
        for index, (start, end) in enumerate(offsets):
            text = INPUT_PREFIX + chunk.content[start:end]
            text_hash = digest(text)
            segment_id = identity({"chunk_id": chunk.id, "start": start, "end": end,
                                   "text_hash": text_hash, "input_version": INPUT_VERSION,
                                   "segment_version": SEGMENT_VERSION})
            segments.append(EmbeddingSegment(segment_id, chunk.id, index, len(offsets),
                                             start, end, text_hash, text))
    return segments
