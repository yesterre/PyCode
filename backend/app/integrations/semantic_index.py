"""Core semantic-index adapter, used inside the existing Project operation lock."""
from __future__ import annotations

from uuid import UUID, uuid4

from backend.app.infrastructure.artifacts import SnapshotArtifactStore
from pycode.chunks import SnapshotBinding
from pycode.embedding import EmbeddingError, EmbeddingProvider, identity, prepare_inputs, validate_result
from pycode.semantic_index import (
    SemanticIdentity, file_hash, read_checkpoint, write_checkpoint, seal_artifact,
)


class SemanticIndexBuilder:
    def __init__(self, artifacts: SnapshotArtifactStore):
        self.artifacts = artifacts

    def build(self, target: SemanticIdentity, chunks, provider: EmbeddingProvider, *,
              batch_size=64, guard=lambda: None) -> dict:
        if type(batch_size) is not int or not 1 <= batch_size <= 128:
            raise ValueError('Invalid embedding batch size')
        if (provider.provider, provider.model, provider.dimension) != (
            target.provider, target.model, target.dimension
        ):
            raise ValueError('Provider identity mismatch')
        guard()
        if ready := self.artifacts.recover_semantic(target, chunks, before_publish=guard):
            return ready
        parent = self.artifacts.semantic_directory(target, create=True)
        root = self.artifacts._project_root(UUID(target.binding.project_id))
        segments = prepare_inputs(chunks, target.max_input_chars)
        staging = parent / f'.staging-{uuid4()}'
        staging.mkdir()
        # A crash leaves a private staging directory, never a ready artifact.
        with (staging / 'vectors.f32').open('wb') as output:
            for offset in range(0, len(segments), batch_size):
                batch = segments[offset:offset + batch_size]
                guard()
                name = 'batch-' + identity({'segments': [s.id for s in batch]})
                checkpoint = parent / name
                data = None
                if checkpoint.exists() or checkpoint.is_symlink():
                    self.artifacts._check_tree(checkpoint, root)
                    try:
                        data = read_checkpoint(checkpoint, target, batch)
                    except (OSError, ValueError, TypeError, KeyError):
                        pass
                if data is None:
                    response = provider.embed_documents([s.text for s in batch])
                    validate_result(response, len(batch), target.provider, target.model, target.dimension)
                    guard()
                    temporary = parent / f'.batch-{uuid4()}'
                    temporary.mkdir()
                    write_checkpoint(temporary, target, batch, response.vectors)
                    guard()
                    self.artifacts._publish_directory(temporary, checkpoint, root,
                        lambda: read_checkpoint(checkpoint, target, batch))
                    data = read_checkpoint(checkpoint, target, batch)
                output.write(data)
        guard()
        seal_artifact(staging, target, segments)
        return self.artifacts.publish_semantic(staging, target, chunks, before_publish=guard)
