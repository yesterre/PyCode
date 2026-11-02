from dataclasses import replace
import json
from pathlib import Path
import struct
from uuid import uuid4

import pytest

from backend.app.infrastructure.artifacts import SnapshotArtifactStore
from backend.app.integrations.semantic_index import SemanticIndexBuilder
from backend.app.core.errors import BackendError
from pycode.chunks import SnapshotBinding, build_chunk_index
from pycode.engine import PyCodeEngine
from pycode.embedding import EmbeddingError
from pycode.semantic_index import SemanticIdentity, file_hash, read_artifact, encode_vectors, write_json
from fake_embedding import FakeEmbeddingProvider


@pytest.fixture
def setup_index(tmp_path):
    binding = SnapshotBinding(str(uuid4()), str(uuid4()), 'a' * 40)
    repo = tmp_path / 'source'
    repo.mkdir()
    (repo / 'code.py').write_text('x = "' + 'a' * 5000 + '"\n', encoding='utf-8')
    chunks = build_chunk_index(PyCodeEngine().build_index(repo, include_chunk_metadata=True), binding)
    (tmp_path / binding.project_id).mkdir()
    store = SnapshotArtifactStore(tmp_path)
    target = SemanticIdentity(binding, 'b' * 64, 'fake', 'fake-v1', 8)
    return store, target, chunks


def test_roundtrip_idempotency_and_model_snapshot_isolation(setup_index):
    store, target, chunks = setup_index
    builder, provider = SemanticIndexBuilder(store), FakeEmbeddingProvider()
    first = builder.build(target, chunks, provider, batch_size=2)
    count = len(provider.calls)
    assert count == 2 and first['vector_count'] == 3
    assert builder.build(target, chunks, provider, batch_size=2) == first
    assert len(provider.calls) == count
    other = replace(target, model='other', dimension=3)
    builder.build(other, chunks, FakeEmbeddingProvider(model='other', dimension=3))
    assert store.read_semantic(target, chunks) == first
    assert other.fingerprint != target.fingerprint
    wrong = replace(target, binding=replace(target.binding, snapshot_id=str(uuid4())))
    with pytest.raises(ValueError): read_artifact(store.semantic_directory(target) / 'ready', wrong, chunks)


def test_partial_batch_failure_checkpoint_resume(setup_index):
    store, target, chunks = setup_index
    builder = SemanticIndexBuilder(store)
    failing = FakeEmbeddingProvider(failures={2: EmbeddingError('embedding_timeout')})
    with pytest.raises(EmbeddingError): builder.build(target, chunks, failing, batch_size=1)
    assert not (store.semantic_directory(target) / 'ready').exists()
    resumed = FakeEmbeddingProvider()
    result = builder.build(target, chunks, resumed, batch_size=1)
    assert len(resumed.calls) == 2
    assert result['vector_count'] == 3


@pytest.mark.parametrize('damage', ['truncate', 'hash', 'nan', 'mapping', 'metadata'])
def test_corruption_rejected_even_when_hash_recomputed(setup_index, damage):
    store, target, chunks = setup_index
    SemanticIndexBuilder(store).build(target, chunks, FakeEmbeddingProvider())
    directory = store.semantic_directory(target) / 'ready'
    manifest = json.loads((directory / 'manifest.json').read_text())
    data = directory / 'vectors.f32'
    if damage == 'truncate': data.write_bytes(data.read_bytes()[:-4])
    if damage == 'hash': data.write_bytes(b'\x00' * data.stat().st_size)
    if damage == 'nan':
        data.write_bytes(struct.pack('<f', float('nan')) + data.read_bytes()[4:])
        manifest['vectors_sha256'] = file_hash(data)
    if damage == 'mapping':
        mapping = json.loads((directory / 'segments.json').read_text())
        mapping[0]['chunk_id'] = 'missing'
        write_json(directory / 'segments.json', mapping)
        manifest['segments_sha256'] = file_hash(directory / 'segments.json')
    if damage == 'metadata': manifest['identity']['dimension'] = 3
    write_json(directory / 'manifest.json', manifest)
    with pytest.raises(BackendError): store.read_semantic(target, chunks)


def test_publication_failure_retains_other_model_and_resumes(setup_index, monkeypatch):
    store, target, chunks = setup_index
    builder = SemanticIndexBuilder(store)
    original = builder.build(target, chunks, FakeEmbeddingProvider())
    other = replace(target, model='other')
    rename = Path.rename
    def fail(source, destination):
        if source.name.startswith('.staging-'):
            raise PermissionError('injected publication failure')
        return rename(source, destination)
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'rename', fail)
        with pytest.raises(BackendError, match='could not be published'):
            builder.build(other, chunks, FakeEmbeddingProvider(model='other'))
    assert store.read_semantic(target, chunks) == original
    resumed = FakeEmbeddingProvider(model='other')
    builder.build(other, chunks, resumed)
    assert resumed.calls == []


def test_backup_restored_and_invalid_float32_rejected(setup_index):
    store, target, chunks = setup_index
    result = SemanticIndexBuilder(store).build(target, chunks, FakeEmbeddingProvider())
    parent = store.semantic_directory(target)
    (parent / 'ready').rename(parent / '.ready.backup-test')
    assert store.recover_semantic(target, chunks) == result
    for value in [float('nan'), float('inf'), 1e100, True]:
        with pytest.raises(ValueError): encode_vectors([[value]], 1)


def test_fingerprint_changes_and_empty_index(setup_index):
    store, target, chunks = setup_index
    variants = [replace(target, chunk_hash='c' * 64), replace(target, max_input_chars=1500),
                replace(target, model='other'), replace(target, dimension=5),
                replace(target, binding=replace(target.binding, commit_sha='c' * 40))]
    assert len({target.fingerprint, *(v.fingerprint for v in variants)}) == 6
    empty = replace(chunks, chunks=[], files=[])
    provider = FakeEmbeddingProvider()
    manifest = SemanticIndexBuilder(store).build(target, empty, provider)
    assert manifest['vector_count'] == 0 and manifest['dimension_source'] == 'model_spec_empty'
    assert not provider.calls


def test_post_publish_failure_restores_same_fingerprint(setup_index, monkeypatch):
    import shutil
    from pycode.embedding import prepare_inputs
    from pycode.semantic_index import seal_artifact
    store, target, chunks = setup_index
    SemanticIndexBuilder(store).build(target, chunks, FakeEmbeddingProvider())
    parent = store.semantic_directory(target)
    before = {p.name: p.read_bytes() for p in (parent / 'ready').iterdir()}
    staging = parent / '.staging-replacement'
    shutil.copytree(parent / 'ready', staging)
    rows = len(prepare_inputs(chunks, target.max_input_chars))
    (staging / 'vectors.f32').write_bytes(encode_vectors([[0.0] * target.dimension] * rows, target.dimension))
    seal_artifact(staging, target, prepare_inputs(chunks, target.max_input_chars))
    read = store.read_semantic
    failed = []
    def reject(*args):
        if not failed:
            failed.append(True)
            raise BackendError('artifacts_unavailable', 'injected post-publish failure')
        return read(*args)
    monkeypatch.setattr(store, 'read_semantic', reject)
    with pytest.raises(BackendError):
        store.publish_semantic(staging, target, chunks, before_publish=lambda: None)
    assert {p.name: p.read_bytes() for p in (parent / 'ready').iterdir()} == before


def test_bad_checkpoint_recomputed_and_hardlink_rejected(setup_index):
    import os
    store, target, chunks = setup_index
    builder = SemanticIndexBuilder(store)
    with pytest.raises(EmbeddingError):
        builder.build(target, chunks, FakeEmbeddingProvider(failures={2: EmbeddingError()}), batch_size=1)
    checkpoint = next(store.semantic_directory(target).glob('batch-*'))
    (checkpoint / 'vectors.f32').write_bytes(b'broken')
    resumed = FakeEmbeddingProvider()
    builder.build(target, chunks, resumed, batch_size=1)
    assert len(resumed.calls) == 3
    data = store.semantic_directory(target) / 'ready' / 'vectors.f32'
    linked = data.parent / 'alias.f32'
    os.link(data, linked)
    with pytest.raises(BackendError): store.read_semantic(target, chunks)
