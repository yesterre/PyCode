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
