"""Deterministic fusion and bounded static Graph tests; no model-quality claims."""
from dataclasses import replace

import pytest

from pycode.graph_builder import build_code_graph
from pycode.models import CodeGraph, GraphNode, GraphEdge
from pycode.query import RetrievalGraph
from pycode.retrieval_evidence import RetrievalContext, RetrievalRequest, Query
from pycode.retriever import retrieve
from pycode.semantic_search import VectorResult, VectorHit, VectorUnavailable
from test_a02a_retrieval import fixture_context


@pytest.fixture
def hybrid_context(tmp_path):
    context = fixture_context(tmp_path, 'def target():\n    return "needle"\n')
    (tmp_path / 'caller.py').write_text('from logic import target\ndef caller():\n    return target()\n', encoding='utf-8')
    (tmp_path / 'second.py').write_text('def second():\n    return 2\n', encoding='utf-8')
    from pycode.engine import PyCodeEngine
    from pycode.chunks import build_chunk_index
    index = PyCodeEngine().build_index(tmp_path, include_chunk_metadata=True)
    chunks = build_chunk_index(index, context.binding)
    context = RetrievalContext.create(tmp_path, index, build_code_graph(index), binding=context.binding, chunks=chunks)
    spans = {c.path:c.span for c in chunks.chunks}
    def query(text, limit):
        return VectorResult(context.binding, (VectorHit('logic.py', spans['logic.py'], .9), VectorHit('second.py',spans['second.py'], .7)))
    return replace(context, retrieval_mode='hybrid', vector_query=query)


def test_channels_fuse_with_independent_scores_and_bounded_static_paths(hybrid_context):
    result = retrieve(RetrievalRequest(Query('needle')), hybrid_context)
    assert result.mode == 'hybrid' and result.pack
    assert result.items[0].path == 'logic.py'
    scores = result.items[0].source_scores
    assert {s.source for s in scores} >= {'keyword','vector'}
    assert result.items[0].score == sum(s.contribution for s in scores)
    assert all(s.contribution == (0.5 if s.source == 'graph' else 1) / (60+s.rank) for i in result.items for s in i.source_scores)
    assert result.graph_stats['edge_checks'] <= hybrid_context.graph_edge_checks
    assert all(i.coverage in {'complete_file','complete_symbol'} for i in result.items)
    assert any(i.graph_path for i in result.items) or result.pack.discarded_count


def test_exact_definition_never_loses_to_vector(hybrid_context):
    def forbidden(*args):
        pytest.fail('Exact route should not spend query Embedding')
    result = retrieve(RetrievalRequest(Query('target')), replace(hybrid_context,vector_query=forbidden))
    assert result.items[0].symbol == 'target' and result.items[0].source == 'exact_symbol'
    assert 'exact_match_priority' in result.items[0].reason


def test_order_is_deterministic_for_graph_and_vector_input_permutations(hybrid_context):
    expected = retrieve(RetrievalRequest(Query('needle')), hybrid_context)
    vector = hybrid_context.vector_query('needle',6)
    graph = hybrid_context.graph
    shuffled = CodeGraph(graph.project_path, list(reversed(graph.nodes)), list(reversed(graph.edges)))
    result = retrieve(RetrievalRequest(Query('needle')), replace(hybrid_context,graph=shuffled,
        vector_query=lambda *a:replace(vector,hits=tuple(reversed(vector.hits)))))
    assert result.items == expected.items
    assert result.pack.summary() == expected.pack.summary()


@pytest.mark.parametrize('direction',['incoming','outgoing','both'])
def test_graph_cycles_high_degree_unknown_edges_respect_each_bound(direction):
    nodes = [GraphNode(f'file:f{i}.py','file',f'f{i}.py',f'f{i}.py') for i in range(100)]
    edges = [GraphEdge(nodes[0].id,n.id,'imports') for n in nodes[1:]]
    edges += [GraphEdge(n.id,nodes[0].id,'calls') for n in nodes[1:]]
    edges += [GraphEdge(nodes[0].id,'missing','calls'), GraphEdge(nodes[0].id,nodes[0].id,'dynamic')]
    view = RetrievalGraph.create(CodeGraph('.',nodes,edges))
    hits, stats = view.expand([nodes[0].id],depth=2,node_limit=4,edge_limit=3,direction=direction)
    assert stats['edge_checks'] <= 3 and stats['visited_nodes'] <= 4 and stats['depth_reached'] <= 2
    assert len({n.id for n,_ in hits}) == len(hits)
    assert all(s['direction'] == direction for _,p in hits for s in p) if direction != 'both' else True
    assert stats['limited']


def test_zero_depth_and_unknown_target_do_not_invent_facts():
    graph=CodeGraph('.', [GraphNode('f','file','f','f.py')], [GraphEdge('f','unknown','calls')])
    view=RetrievalGraph.create(graph)
    hits, stats=view.expand(['f'],depth=0,node_limit=1,edge_limit=1,direction='both')
    assert hits == [] and stats['edge_checks'] == 0
    hits, stats=view.expand(['f'],depth=2,node_limit=4,edge_limit=4,direction='outgoing')
    assert hits == [] and stats['unknown_relations'] == 1


def test_graph_index_is_lazy_and_reused_in_one_trusted_context(hybrid_context):
    assert 'graph_view' not in hybrid_context.__dict__
    retrieve(RetrievalRequest(Query('needle')),hybrid_context)
    view=hybrid_context.graph_view
    retrieve(RetrievalRequest(Query('needle')),hybrid_context)
    assert hybrid_context.graph_view is view
    legacy=replace(hybrid_context,retrieval_mode='keyword')
    retrieve(RetrievalRequest(Query('needle')),legacy)
    assert 'graph_view' not in legacy.__dict__


def test_provider_failure_and_foreign_binding_degrade_without_mixing(hybrid_context):
    for callback in (lambda *a: (_ for _ in ()).throw(VectorUnavailable('vector_provider_failed')),
                     lambda *a: replace(hybrid_context.vector_query('q',6),binding=replace(hybrid_context.binding,commit_sha='b'*40))):
        result=retrieve(RetrievalRequest(Query('needle')),replace(hybrid_context,vector_query=callback))
        assert result.items and result.status == 'degraded'
        assert any(d.startswith('vector_') for d in result.diagnostics)
        assert all(i.commit_sha == hybrid_context.binding.commit_sha for i in result.items)
        assert not any(s.source=='vector' for i in result.items for s in i.source_scores)


@pytest.mark.parametrize('depth,nodes,checks', [(-1,2,3),(5,2,3),(1,0,3),(1,2,0),(True,2,3)])
def test_invalid_graph_configuration_rejected(depth,nodes,checks):
    with pytest.raises(ValueError):
        RetrievalGraph.create(CodeGraph('.')).expand([],depth=depth,node_limit=nodes,edge_limit=checks,direction='both')


@pytest.fixture
def intent_context(tmp_path):
    """A target with independent importers/dependencies and no conventional entry."""
    sources = {
        'a_reader.py': 'from target import work\ndef consume():\n    return work()\n',
        'target.py': 'from z_model import Model\ndef work():\n    return Model()\n',
        'z_model.py': 'class Model:\n    pass\n',
        'zz_unrelated.py': 'def unrelated():\n    return 42\n',
    }
    for path, source in sources.items():
        (tmp_path / path).write_text(source, encoding='utf-8')
    from pycode.engine import PyCodeEngine
    from pycode.chunks import SnapshotBinding, build_chunk_index
    from uuid import uuid4
    index = PyCodeEngine().build_index(tmp_path, include_chunk_metadata=True)
    binding = SnapshotBinding(str(uuid4()), str(uuid4()), 'a' * 40)
    return RetrievalContext.create(tmp_path, index, build_code_graph(index), binding=binding,
                                   chunks=build_chunk_index(index, binding))


def test_hybrid_onboard_without_entry_retains_reading_hints_and_uncertainty(intent_context):
    query = RetrievalRequest(Query('生成项目新手阅读顺序', 'onboard'))
    legacy = retrieve(query, intent_context)
    def unavailable(*args):
        raise VectorUnavailable('vector_provider_failed')
    hybrid = retrieve(query, replace(intent_context, retrieval_mode='hybrid', vector_query=unavailable))
    assert [i.path for i in hybrid.items] == [i.path for i in legacy.items] == ['a_reader.py','target.py','z_model.py']
    assert 'entry_unknown_reading_order_only' in hybrid.diagnostics
    assert all('not_proven_runtime_entry' in i.reason or i.role == 'dependency' for i in hybrid.items)
    assert hybrid.mode == 'hybrid' and hybrid.pack


@pytest.mark.parametrize('intent', ['impact','dependency'])
@pytest.mark.parametrize('explicit_target', [True, False])
def test_hybrid_target_strategies_preserve_both_import_directions_even_without_expansion(
    intent_context, intent, explicit_target,
):
    request = RetrievalRequest(Query('分析 target.py 的结构关系', intent, 'target.py' if explicit_target else None), 8)
    legacy = retrieve(request, intent_context)
    hybrid = retrieve(request, replace(intent_context, retrieval_mode='hybrid', graph_depth=0, graph_edge_checks=1))
    assert [i.path for i in hybrid.items] == [i.path for i in legacy.items] == ['target.py','a_reader.py','z_model.py']
    assert 'file:a_reader.py --imports--> file:target.py' in hybrid.evidence
    assert 'file:target.py --imports--> file:z_model.py' in hybrid.evidence
    assert hybrid.items[0].source == 'exact_path'
    assert all(i.commit_sha == intent_context.binding.commit_sha for i in hybrid.items)


def test_hybrid_entry_without_known_entry_never_promotes_vector_match(intent_context):
    unrelated = next(c.span for c in intent_context.chunks.chunks if c.path == 'zz_unrelated.py')
    def vector(*args):
        return VectorResult(intent_context.binding, (VectorHit('zz_unrelated.py', unrelated, 1.0),))
    result = retrieve(RetrievalRequest(Query('入口在哪里', 'entry')),
                      replace(intent_context, retrieval_mode='hybrid', vector_query=vector))
    assert result.items == []


@pytest.mark.parametrize('intent', ['entry','onboard'])
def test_hybrid_entry_strategies_preserve_whole_entry_file_and_skip_provider(tmp_path, intent):
    from test_retriever import _sample_project
    root, index, graph = _sample_project(tmp_path)
    base = RetrievalContext.create(root, index, graph)
    calls = []
    def unavailable(*args):
        calls.append(args)
        raise VectorUnavailable('vector_provider_failed')
    request = RetrievalRequest(Query('return 启动入口', intent))
    legacy = retrieve(request, base)
    result = retrieve(request, replace(base, retrieval_mode='hybrid', vector_query=unavailable))
    assert [i.path for i in result.items] == [i.path for i in legacy.items]
    assert [(i.start_line,i.end_line,i.snippet) for i in result.items] == [
        (i.start_line,i.end_line,i.snippet) for i in legacy.items]
    assert not calls


@pytest.mark.parametrize('intent', ['impact','dependency','explain'])
def test_hybrid_unique_basename_target_uses_original_resolution(tmp_path, intent):
    nested = tmp_path / 'package'
    nested.mkdir()
    (nested / 'feature.py').write_text('def handler():\n    return 1\n', encoding='utf-8')
    from pycode.engine import PyCodeEngine
    index = PyCodeEngine().build_index(tmp_path)
    # Exercise the existing canonical relative-path contract. Raw Windows
    # FileInfo separators are a pre-existing resolver limitation, not this fix.
    for file in index.files:
        file.path = file.path.replace('\\', '/')
    base = RetrievalContext.create(tmp_path, index, build_code_graph(index))
    request = RetrievalRequest(Query('检查 feature.py 文件', intent))
    assert retrieve(request, base).items[0].source == 'exact_path'
    result = retrieve(request, replace(base, retrieval_mode='hybrid'))
    assert result.items[0].path == 'package/feature.py' and result.items[0].source == 'exact_path'


def test_hybrid_structural_intent_still_obeys_evidence_budget(intent_context):
    result = retrieve(RetrievalRequest(Query('target.py','impact','target.py'),8),
                      replace(intent_context,retrieval_mode='hybrid',token_budget=4096))
    from pycode.evidence_pack import measure
    assert result.pack.estimated_tokens <= 4096 and measure(result) <= result.pack.estimated_tokens
    if result.pack.discarded_count:
        assert result.pack.reason_counts
