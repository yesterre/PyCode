"""Versioned float32 semantic artifacts. No retrieval or filesystem publication."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import struct

from pycode.chunks import ChunkIndex, SnapshotBinding
from pycode.embedding import INPUT_VERSION, SEGMENT_VERSION, EmbeddingSegment, identity, prepare_inputs

FORMAT_VERSION = 1


def file_hash(path: Path) -> str:
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


@dataclass(frozen=True)
class SemanticIdentity:
    binding: SnapshotBinding
    chunk_hash: str
    provider: str
    model: str
    dimension: int
    max_input_chars: int = 1800
    endpoint: str = ''
    chunk_schema: int = 1
    input_version: int = INPUT_VERSION
    segment_version: int = SEGMENT_VERSION
    format_version: int = FORMAT_VERSION
    normalization: str = 'none'

    def __post_init__(self):
        if (len(self.chunk_hash) != 64 or any(c not in '0123456789abcdef' for c in self.chunk_hash)
                or type(self.dimension) is not int or not 1 <= self.dimension <= 65536
                or not self.provider or not self.model
                or any(type(v) is not int for v in (self.chunk_schema, self.input_version,
                                                    self.segment_version, self.format_version))
                or self.format_version != FORMAT_VERSION or self.input_version != INPUT_VERSION
                or self.segment_version != SEGMENT_VERSION or self.normalization != 'none'
                or type(self.max_input_chars) is not int or not 13 < self.max_input_chars <= 2000):
            raise ValueError('Invalid semantic identity')

    @property
    def fingerprint(self) -> str:
        return identity(asdict(self))


def write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(',', ':'),
                               ensure_ascii=False, allow_nan=False), encoding='utf-8')


def encode_vectors(vectors: list[list[float]], dimension: int) -> bytes:
    result = bytearray()
    for row in vectors:
        if len(row) != dimension or any(type(v) not in (int, float) or not math.isfinite(v) for v in row):
            raise ValueError('Invalid vector')
        try:
            encoded = struct.pack('<' + 'f' * dimension, *row)
        except (OverflowError, struct.error):
            raise ValueError('Invalid float32 vector') from None
        if any(not math.isfinite(v) for (v,) in struct.iter_unpack('<f', encoded)):
            raise ValueError('Invalid float32 vector')
        result.extend(encoded)
    return bytes(result)


def validate_binary(path: Path, rows: int, dimension: int) -> None:
    if path.stat().st_size != rows * dimension * 4:
        raise ValueError('Vector size mismatch')
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(65536), b''):
            if any(not math.isfinite(v) for (v,) in struct.iter_unpack('<f', block)):
                raise ValueError('Nonfinite vector')


def seal_artifact(directory: Path, target: SemanticIdentity,
                  segments: list[EmbeddingSegment]) -> dict:
    """Seal a fully written vectors.f32 file; caller owns staging and publication."""
    validate_binary(directory / 'vectors.f32', len(segments), target.dimension)
    write_json(directory / 'segments.json', [segment.metadata() for segment in segments])
    manifest = {
        'identity': asdict(target), 'fingerprint': target.fingerprint,
        'dtype': 'float32', 'byte_order': 'little', 'vector_count': len(segments),
        'segment_count': len(segments), 'byte_count': len(segments) * target.dimension * 4,
        'dimension_source': 'validated_response' if segments else 'model_spec_empty',
        'vectors_sha256': file_hash(directory / 'vectors.f32'),
        'segments_sha256': file_hash(directory / 'segments.json'),
        'built_at': datetime.now(timezone.utc).isoformat(),
    }
    write_json(directory / 'manifest.json', manifest)
    return manifest


def read_artifact(directory: Path, target: SemanticIdentity, chunks: ChunkIndex) -> dict:
    if chunks.binding != target.binding or chunks.schema_version != target.chunk_schema:
        raise ValueError('Chunk binding mismatch')
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    segments = prepare_inputs(chunks, target.max_input_chars)
    expected = {
        'identity': asdict(target), 'fingerprint': target.fingerprint,
        'dtype': 'float32', 'byte_order': 'little', 'vector_count': len(segments),
        'segment_count': len(segments), 'byte_count': len(segments) * target.dimension * 4,
        'dimension_source': 'validated_response' if segments else 'model_spec_empty',
        'vectors_sha256': file_hash(directory / 'vectors.f32'),
        'segments_sha256': file_hash(directory / 'segments.json'),
        'built_at': manifest.get('built_at'),
    }
    # Do not accept booleans as counts via Python's bool/int equality.
    if any(type(manifest.get(k)) is not int for k in ('vector_count', 'segment_count', 'byte_count')):
        raise ValueError('Invalid semantic counts')
    if identity(manifest) != identity(expected) or not isinstance(manifest.get('built_at'), str):
        raise ValueError('Semantic manifest mismatch')
    if datetime.fromisoformat(manifest['built_at']).tzinfo is None:
        raise ValueError('Semantic timestamp must include timezone')
    mapping = json.loads((directory / 'segments.json').read_text(encoding='utf-8'))
    if identity({'mapping': mapping}) != identity({'mapping': [segment.metadata() for segment in segments]}):
        raise ValueError('Semantic mapping mismatch')
    validate_binary(directory / 'vectors.f32', len(segments), target.dimension)
    return manifest


def read_checkpoint(directory: Path, target: SemanticIdentity,
                    segments: list[EmbeddingSegment]) -> bytes:
    manifest = json.loads((directory / 'batch.json').read_text(encoding='utf-8'))
    data = directory / 'vectors.f32'
    if identity(manifest) != identity({'fingerprint': target.fingerprint,
                    'segments': [s.metadata() for s in segments], 'sha256': file_hash(data)}):
        raise ValueError('Checkpoint identity mismatch')
    validate_binary(data, len(segments), target.dimension)
    return data.read_bytes()


def write_checkpoint(directory: Path, target: SemanticIdentity,
                     segments: list[EmbeddingSegment], vectors: list[list[float]]) -> None:
    if len(vectors) != len(segments):
        raise ValueError('Checkpoint count mismatch')
    (directory / 'vectors.f32').write_bytes(encode_vectors(vectors, target.dimension))
    write_json(directory / 'batch.json', {'fingerprint': target.fingerprint,
        'segments': [s.metadata() for s in segments], 'sha256': file_hash(directory / 'vectors.f32')})
