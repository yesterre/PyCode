"""Vector engineering invariants; deterministic doubles make no quality claim."""
from dataclasses import replace
from uuid import uuid4

import pytest

from backend.app.config import RetrievalSettings
from pycode.embedding import EmbeddingError, EmbeddingResult, prepare_inputs
from pycode.retrieval_evidence import Query, RetrievalRequest
from pycode.retriever import retrieve, retrieve_impact
from pycode.semantic_index import SemanticIdentity, encode_vectors, seal_artifact
from pycode.semantic_search import ReadOnlyVectorIndex, VectorUnavailable
from pycode.tools import ToolContext, retrieve_context
from test_a02a_retrieval import fixture_context, assert_exact


@pytest.fixture
def vector_data(tmp_path):
    source = '@decorate\ndef lengthy():\n' + "    value = 'long payload to force several embedding segments'\n" * 250 + '    return value\n'
    source += 'def other():\n    return 2\n'
    context = fixture_context(tmp_path / 'repo', source)
    target = SemanticIdentity(context.binding, 'b' * 64, 'fake', 'fake-v1', 2)
    directory = tmp_path / 'ready'
    directory.mkdir()
    segments = prepare_inputs(context.chunks)
    chunks = {c.id: c for c in context.chunks.chunks}
    rows = [[1.0, 0.0] if chunks[s.chunk_id].span.qualified_name == 'lengthy' else [0.0, 1.0] for s in segments]
    (directory / 'vectors.f32').write_bytes(encode_vectors(rows, 2))
    seal_artifact(directory, target, segments)
    return context, target, directory, source


def response(**kwargs):
    return replace(EmbeddingResult('fake', 'fake-v1', 2, 1, [[1.0, 0.0]]), **kwargs)


def test_segments_and_chunk_parts_aggregate_before_top_k_with_complete_evidence(vector_data, monkeypatch):
    context, target, directory, source = vector_data
    def forbidden(*args, **kwargs):
        pytest.fail('Query must reuse published data without scanning or parsing')
    monkeypatch.setattr('ast.parse', forbidden)
    monkeypatch.setattr('pycode.scanner.scan_python_files', forbidden)
    index = ReadOnlyVectorIndex.load(directory, target, context.chunks)
    assert len(index.rows) > len(context.chunks.chunks) > 2
    hits = index.search(response(), 2)
    assert [h.span.qualified_name for h in hits.hits] == ['lengthy', 'other']
    bound = replace(context, retrieval_mode='vector', vector_query=lambda text, k: index.search(response(), k))
    result = retrieve(RetrievalRequest(Query('如何完成这项处理'), 2), bound)
    assert [i.symbol for i in result.items] == ['lengthy', 'other']
    assert all(i.source == 'vector' for i in result.items)
    assert len(result.items[0].chunk_ids) > 1
    assert_exact(result.items[0], source)
    tool = retrieve_context(ToolContext(context.root, retrieval_context=bound), '如何完成这项处理')
    assert tool.ok and tool.data['items'][0]['snippet'] == result.items[0].snippet
    assert tool.data['items'][0]['snippet_truncated'] is False


def test_ties_are_stable_and_do_not_use_segment_order(vector_data):
    context, target, directory, _ = vector_data
    index = ReadOnlyVectorIndex.load(directory, target, context.chunks)
    tied = response(vectors=[[1.0, 1.0]])
    assert index.search(tied, 20) == replace(index, rows=tuple(reversed(index.rows))).search(tied, 20)
    assert len(index.search(tied, 1).hits) == 1


@pytest.mark.parametrize('field,value', [
    ('provider', 'other'), ('model', 'other'), ('dimension', 3), ('input_count', 2),
    ('vectors', [[0.0, 0.0]]), ('vectors', [[float('nan'), 1.0]]),
    ('vectors', [[float('inf'), 1.0]]), ('vectors', [[1.0]]),
])
def test_query_result_is_strictly_validated(vector_data, field, value):
    context, target, directory, _ = vector_data
    index = ReadOnlyVectorIndex.load(directory, target, context.chunks)
    with pytest.raises((ValueError, EmbeddingError)):
        index.search(response(**{field: value}), 2)


@pytest.mark.parametrize('field,value', [('model', 'other'), ('dimension', 3), ('format_version', 2),
                                         ('chunk_hash', 'c' * 64)])
def test_artifact_identity_cannot_be_substituted(vector_data, field, value):
    context, target, directory, _ = vector_data
    with pytest.raises(ValueError):
        ReadOnlyVectorIndex.load(directory, replace(target, **{field: value}), context.chunks)


@pytest.mark.parametrize('field', ['project_id', 'snapshot_id', 'commit_sha'])
def test_even_same_commit_cannot_cross_project_or_snapshot(vector_data, field):
    context, target, directory, _ = vector_data
    binding = replace(target.binding, **{field: 'c' * 40 if field == 'commit_sha' else str(uuid4())})
    with pytest.raises(ValueError):
        ReadOnlyVectorIndex.load(directory, replace(target, binding=binding), context.chunks)


@pytest.mark.parametrize('filename', ['manifest.json', 'segments.json', 'vectors.f32'])
def test_corruption_rejected_before_query(vector_data, filename):
    context, target, directory, _ = vector_data
    (directory / filename).write_bytes(b'bad')
    with pytest.raises((ValueError, OSError)):
        ReadOnlyVectorIndex.load(directory, target, context.chunks)


def test_search_uses_captured_validated_bytes(vector_data, monkeypatch):
    context, target, directory, _ = vector_data
    from pycode import semantic_search
    original = semantic_search.read_artifact
    def change_after_validation(*args):
        manifest = original(*args)
        (directory / 'vectors.f32').write_bytes(b'changed')
        return manifest
    monkeypatch.setattr(semantic_search, 'read_artifact', change_after_validation)
    with pytest.raises(ValueError, match='changed'):
        ReadOnlyVectorIndex.load(directory, target, context.chunks)


def test_zero_document_vectors_rejected_and_empty_index_valid(vector_data):
    context, target, directory, _ = vector_data
    segments = prepare_inputs(context.chunks)
    (directory / 'vectors.f32').write_bytes(encode_vectors([[0, 0]] * len(segments), 2))
    seal_artifact(directory, target, segments)
    with pytest.raises(ValueError, match='cosine'):
        ReadOnlyVectorIndex.load(directory, target, context.chunks)
    empty = replace(context.chunks, files=[], chunks=[])
    (directory / 'vectors.f32').write_bytes(b'')
    seal_artifact(directory, target, [])
    assert ReadOnlyVectorIndex.load(directory, target, empty).search(response(), 2).hits == ()


def test_exact_symbols_and_impact_never_call_vector(vector_data):
    context, _, _, _ = vector_data
    def forbidden(*args):
        pytest.fail('Exact/Impact must keep existing behavior')
    context = replace(context, retrieval_mode='vector', vector_query=forbidden)
    assert retrieve(RetrievalRequest(Query('lengthy')), context).items[0].source == 'exact_symbol'
    result = retrieve_impact('logic.py', context.root, context.index, context.graph, context=context)
    assert result.items[0].source == 'exact_path'


def test_unavailable_and_forged_binding_fall_back_explicitly(vector_data):
    context, target, directory, _ = vector_data
    index = ReadOnlyVectorIndex.load(directory, target, context.chunks)
    def unavailable(*args):
        raise VectorUnavailable('arbitrary secret error')
    for callback, diagnostic in [(unavailable, 'vector_unavailable'),
        (lambda text, k: replace(index.search(response(), k), binding=replace(context.binding, snapshot_id=str(uuid4()))), 'vector_binding_mismatch')]:
        result = retrieve(RetrievalRequest(Query('value')), replace(context, retrieval_mode='vector', vector_query=callback))
        assert result.items and result.status == 'degraded'
        assert diagnostic in result.diagnostics and all(i.source == 'keyword' for i in result.items)


def test_failed_vector_with_empty_keyword_fallback_is_degraded_not_clean_miss(vector_data):
    context, _, _, _ = vector_data
    def missing(*args):
        raise VectorUnavailable('vector_artifact_unavailable')
    result = retrieve(RetrievalRequest(Query('zzzznomatch')), replace(context, retrieval_mode='vector', vector_query=missing))
    assert not result.items and result.status == 'degraded'
    assert 'vector_artifact_unavailable' in result.diagnostics


@pytest.mark.parametrize('mode,k', [('unsupported', 6), ('vector', 0), ('vector', 21), ('vector', True)])
def test_config_rejects_invalid_values(mode, k):
    with pytest.raises(ValueError):
        RetrievalSettings(mode, k)
