from dataclasses import replace

import pytest

from pycode.evidence_pack import build_pack, measure, render_evidence, tool_items, encoded
from pycode.prompt_builder import build_code_qa_prompt
from pycode.retrieval_evidence import Query, RetrievalRequest
from pycode.retriever import retrieve
from pycode.tools import ToolContext, retrieve_context
from test_a02a_retrieval import fixture_context, assert_exact


def test_oversized_definition_is_discarded_whole_and_replaced_by_fitting_code(tmp_path):
    source = 'def enormous():\n    payload = "' + '中文\\\"' * 5000 + '"\n    return payload\n'
    source += 'def tiny():\n    return "payload"\n'
    context=replace(fixture_context(tmp_path,source),token_budget=4096)
    result=retrieve(RetrievalRequest(Query('payload')),context)
    assert result.pack.estimated_tokens <= 4096 and measure(result) <= result.pack.estimated_tokens
    assert 'token_budget' in dict(result.pack.reason_counts)
    assert all(i.symbol != 'enormous' for i in result.items)
    assert all(i.coverage != 'partial' for i in result.items)
    for item in result.items:
        assert_exact(item,source)
    assert 'evidence_budget_limited' in result.diagnostics


def test_prompt_and_tool_consume_identical_selected_ranges_without_recropping(tmp_path):
    source='@decorate\ndef longish():\n' + '    value = 1\n'*150 + '    return value\n'
    context=replace(fixture_context(tmp_path,source),token_budget=20000)
    result=retrieve(RetrievalRequest(Query('longish')),context)
    tool=retrieve_context(ToolContext(tmp_path,retrieval_context=context),'longish',max_snippet_chars=5)
    assert tool.ok and result.items
    assert tool.data['items'] == tool_items(result.items)
    assert result.items[0].snippet in build_code_qa_prompt(result)
    assert len(result.items[0].chunk_ids)>1
    assert result.pack.estimated_tokens >= len(render_evidence(result.items).encode('utf-8'))
    assert all(not i['snippet_truncated'] for i in tool.data['items'])


def test_exact_boundary_and_metadata_cost_and_snapshot_binding(tmp_path):
    context=fixture_context(tmp_path,'def tiny():\n    return 1\n')
    result=retrieve(RetrievalRequest(Query('tiny')),context)
    budget=max(4096,result.pack.estimated_tokens)
    repacked=build_pack(result,budget,6)
    assert measure(repacked)<=budget
    foreign=replace(result.items[0],snapshot_id='foreign')
    with pytest.raises(ValueError,match='Snapshot'):
        build_pack(replace(result,items=[foreign]),budget,6)


def test_overlapping_parent_and_child_are_explicit_and_diagnostics_are_bounded(tmp_path):
    context=fixture_context(tmp_path,'def outer():\n    def inner():\n        return 1\n    return inner()\n')
    result=retrieve(RetrievalRequest(Query('outer')),context)
    parent=result.items[0]
    candidates=[parent,replace(parent,symbol='duplicate',source='hybrid')]*40
    pack=build_pack(replace(result,items=candidates,mode='hybrid'),10000,6)
    assert len(pack.items)==1 and dict(pack.pack.reason_counts)['overlap']==79
    assert len(pack.pack.discarded)<=20 and pack.pack.summary()['discarded_omitted']>=59
    assert measure(pack)<=10000


@pytest.mark.parametrize('budget',[0,True,4095,1048577])
def test_invalid_budget_is_not_silently_coerced(tmp_path,budget):
    context=fixture_context(tmp_path,'x=1\n')
    with pytest.raises(ValueError):
        retrieve(RetrievalRequest(Query('x')),replace(context,token_budget=budget))
