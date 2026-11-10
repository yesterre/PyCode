"""Q-T03 provenance rendering, source invariants and evaluation boundaries."""
from copy import deepcopy
from dataclasses import replace

import pytest

from pycode.agent.evidence import verify_context_answer
from pycode.evidence_pack import render_evidence,measure,tool_items
from pycode.prompt_builder import build_code_qa_prompt
from pycode.retrieval_evidence import Query,RetrievalRequest
from pycode.retriever import retrieve
from pycode.tools import ToolContext,retrieve_context
from scripts.benchmarks.q_t03_evidence_quality import disclosure,locked_inputs,score,compare
from test_a03_receivers import build


def test_real_hybrid_graph_proof_survives_fast_prompt_without_changing_tool_source(tmp_path):
    _,graph,context=build(tmp_path,{
        'entry.py':'from worker import transform\ndef run(value):\n    return transform(value)\n',
        'worker.py':'def transform(value):\n    return str(value)\n',
    })
    context=replace(context,retrieval_mode='hybrid',token_budget=32768)
    result=retrieve(RetrievalRequest(Query('run')),context)
    prompt=build_code_qa_prompt(result)
    graph_items=[item for item in result.items if item.graph_path]
    assert graph_items
    assert any(step.get('basis')=='import_binding_chain' for item in graph_items for step in item.graph_path)
    for item in graph_items:
        for step in item.graph_path:
            assert 'resolution='+step['resolution'] in prompt
            assert 'basis='+step['basis'] in prompt
        assert item.snippet in prompt
    tool=retrieve_context(ToolContext(context.root,retrieval_context=context),'run')
    assert tool.ok and tool.data['items']==tool_items(result.items)
    assert measure(result)<=result.pack.estimated_tokens<=32768
    assert graph.schema_version==4 and graph==context.graph
    assert 'runtime=unverified' in prompt


@pytest.mark.parametrize('resolution', ['static','declaration','unknown','ambiguous','legacy',None,'future'])
def test_relation_resolution_is_disclosed_without_promoting_unknown_or_legacy(tmp_path,resolution):
    _,_,context=build(tmp_path,{'a.py':'def work():\n    return 1\n'})
    item=retrieve(RetrievalRequest(Query('work')),context).items[0]
    step=dict(source='func:caller.py:run',target='func:a.py:work',relation='calls',
              basis='recorded_proof',path='caller.py',start_line=3,end_line=3)
    if resolution is not None:step['resolution']=resolution
    edge="func:caller.py:run --calls--> func:a.py:work"
    item=replace(item,graph_path=(step,),edges=(edge,))
    before=deepcopy(item.to_dict());text=render_evidence([item])
    expected=resolution if resolution in {'static','declaration','unknown','ambiguous','legacy'} else 'unavailable'
    assert 'resolution='+expected in text and 'basis=recorded_proof' in text
    assert 'runtime=unverified' in text
    assert 'graph_site_path=caller.py; graph_site_lines=3-3' in text
    assert 'caller.py:3' not in text  # Metadata is not a newly delivered code excerpt.
    assert text.count(edge)==1 and item.snippet in text
    assert item.to_dict()==before
    assert disclosure([item.to_dict()],[text])[0]['disclosed']


def test_flat_legacy_and_additional_edges_remain_unproved_without_disappearing(tmp_path):
    _,_,context=build(tmp_path,{'a.py':'def work():\n    pass\n'})
    item=retrieve(RetrievalRequest(Query('work')),context).items[0]
    unknown='func:a.py:work --unresolved_call--> unresolved:dynamic'
    item=replace(item,graph_path=(),edges=(unknown,))
    text=render_evidence([item])
    assert unknown in text and 'proof=unavailable' in text and 'runtime=unverified' in text
    assert 'resolution=static' not in text
    assert item.snippet in text


def test_score_separates_candidates_selected_and_rendered_input_without_answer_claims():
    one=dict(path='a.py',symbol='first',start_line=1,end_line=3)
    two=dict(path='b.py',symbol='second',start_line=5,end_line=7)
    truth=dict(top_k=6,evidence=[dict(path='a.py',symbol='first',minimal_ranges=[[2,3]]),
                                dict(path='b.py',symbol='second',minimal_ranges=[[6,7]])])
    row=dict(candidates=[one,two],selected=[one],fast_visible=[one],agent_visible=[{'source':'memory_hint'}])
    result=score(row,truth)
    assert result['candidate']['evidence_lines']['numerator']==2
    assert result['selected']['evidence_lines']==dict(numerator=1,denominator=2,value=.5)
    assert result['fast_rendered']['evidence_lines']['value']==.5
    assert result['agent_rendered']['evidence_lines']['numerator']==0
    assert 'factual_correctness' not in result


def test_valid_address_verification_does_not_certify_an_unsupported_natural_language_claim():
    report={'references':[dict(path='a.py',start_line=1,end_line=3,source='snapshot_source')]}
    result=verify_context_answer('a.py:1-3 proves that this retries exactly 999 times.',report)
    assert result['status']=='checked'
    assert result['natural_language_verified'] is False


def test_frozen_qt03_protocol_and_comparison_reject_changed_conditions():
    protocol,questions,lock=locked_inputs()
    assert len(questions)==40 and sum(q['split']=='validation' for q in questions)==12
    assert protocol['model']['real_calls']==0 and not protocol['model']['fake_answers']
    assert protocol['retrieval']['token_budget']==32768
    assert protocol['baseline_commit']=='0dcb368748cbd62d66b316029f78af7d0cca5e5d'
    left=dict(protocol=protocol,questions=questions,input_lock=lock,environment={},source_hashes={},graph_hashes={})
    right=deepcopy(left);right['protocol']['retrieval']['token_budget']+=1
    with pytest.raises(ValueError,match='Non-comparable'):
        compare(left,right)
