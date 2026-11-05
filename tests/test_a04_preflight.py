from dataclasses import asdict, replace
import json
from pathlib import Path

import pytest

from test_a02a_retrieval import fixture_context
from pycode.question import prepare_question, route_question, InitialEvidence, assess_evidence
from pycode.engine import PyCodeEngine
from pycode.tools import ToolContext, retrieve_context
from pycode.agent.types import AgentTask
from pycode.agent.llm_planner import build_llm_planner_prompt
from pycode.agent.prompts import build_agent_summary_context, render_agent_prompt
from pycode.semantic_search import VectorUnavailable


def decision(text):return asdict(route_question(text))


@pytest.mark.parametrize('case',json.loads((Path(__file__).parent/'fixtures/a04/routing_cases.json').read_text()))
def test_fixed_question_route_and_preflight_contract(tmp_path,case):
    ctx=fixture_context(tmp_path,'def known():\n    return 42\n',filename=case.get('filename','main.py'))
    d=decision(case['question']); assert d['strategy']==case['rule']
    if d['strategy']=='agent':return
    pre=prepare_question(case['question'],d,ctx)
    assert pre.status==case['preflight']
    assert ('agent' if pre.reasons else 'fast')==case['final']


def test_same_pack_in_fast_prompt_and_agent_first_context_without_tool_fabrication(tmp_path):
    ctx=fixture_context(tmp_path,'def work():\n    return 42\n',filename='main.py')
    pre=prepare_question('explain main.py',decision('explain main.py'),ctx)
    class LLM:
        prompts=[]
        def generate(self,prompt):self.prompts.append(prompt);return 'fake answer'
    llm=LLM()
    answer=PyCodeEngine().answer_question(tmp_path,'explain main.py',decision=decision('explain main.py'),
        retrieval_context=ctx,llm_client=llm,preflight=pre)
    assert answer.retrieval is pre.initial_evidence.result
    task=AgentTask('original task',tmp_path,initial_evidence=pre.initial_evidence)
    prompt=build_llm_planner_prompt(task,tools={})
    context=build_agent_summary_context(task,[],[],load_tasks=False)
    rendered=render_agent_prompt(context)
    assert context.section_names().count('initial_evidence')==1
    assert 'No tools were executed.' in rendered
    for item in answer.retrieval.items:
        assert item.snippet in llm.prompts[-1]
        payload=json.loads(prompt.split('Planning input:\n')[-1])
        assert item.snippet in payload['initial_evidence']['items'][0]['snippet']
        assert json.dumps(item.snippet)[1:-1] in rendered
    assert pre.initial_evidence.digest in prompt and ctx.binding.snapshot_id in prompt


def test_single_entry_reuse_never_embeds_original_query_again_and_different_query_can_search(tmp_path):
    ctx=fixture_context(tmp_path,'def work():\n    return 42\n',filename='main.py')
    calls=[]
    def unavailable(text,limit):calls.append(text);raise VectorUnavailable('vector_provider_failed')
    ctx=replace(ctx,retrieval_mode='vector',vector_query=unavailable)
    d=dict(strategy='fast',intent='general',target=None)
    pre=prepare_question('unseen query',d,ctx); assert calls==['unseen query']
    toolctx=ToolContext(tmp_path,retrieval_context=ctx,initial_evidence=pre.initial_evidence)
    for _ in range(2):
        result=retrieve_context(toolctx,'unseen query',max_snippet_chars=1)
        assert result.ok and result.data['initial_evidence_reused'] is True
        assert result.data['retrieval']==pre.initial_evidence.result.summary()
    assert calls==['unseen query']
    assert retrieve_context(toolctx,'another query').ok
    assert calls==['unseen query','another query']
    denied=retrieve_context(toolctx,'unseen query',index_path='fake.json')
    assert not denied.ok and calls==['unseen query','another query']


def test_seed_rejects_context_identity_cross_snapshot_and_content_tampering(tmp_path):
    ctx=fixture_context(tmp_path,'def work():\n    return 42\n',filename='main.py')
    pre=prepare_question('explain main.py',decision('explain main.py'),ctx)
    with pytest.raises(ValueError):pre.initial_evidence.validate(replace(ctx))
    altered=replace(pre.initial_evidence.result,items=[replace(pre.initial_evidence.result.items[0],snippet='forged')])
    seed=replace(pre.initial_evidence,result=altered)
    with pytest.raises(ValueError):seed.validate(ctx)
    assert not retrieve_context(ToolContext(tmp_path,retrieval_context=ctx,initial_evidence=seed),'explain main.py').ok


def test_vector_degradation_does_not_erase_sufficient_exact_code_and_budget_is_explicit(tmp_path):
    ctx=fixture_context(tmp_path,'def work():\n    return 42\n',filename='main.py')
    pre=prepare_question('explain main.py',decision('explain main.py'),ctx)
    degraded=replace(pre.initial_evidence.result,status='degraded',diagnostics=('vector_provider_failed',))
    assert assess_evidence(degraded,ctx)==()
    partial=replace(degraded,items=[replace(degraded.items[0],coverage='unavailable')])
    assert 'partial_coverage' in assess_evidence(partial,ctx)
    budget=replace(degraded,pack=replace(degraded.pack,discarded_count=1,reason_counts=(('token_budget',1),)))
    assert 'budget_limited' in assess_evidence(budget,ctx)
    overlap=replace(degraded,pack=replace(degraded.pack,discarded_count=1,reason_counts=(('overlap',1),)))
    assert assess_evidence(overlap,ctx)==()


def test_unknown_static_relations_upgrade_without_turning_them_into_runtime_facts(tmp_path):
    ctx=fixture_context(tmp_path,'def work(value):\n    return value.dynamic()\n',filename='main.py')
    pre=prepare_question('impact main.py',decision('impact main.py'),ctx)
    assert pre.status=='insufficient' and 'relations_unconfirmed' in pre.reasons


def test_long_nested_decorated_chunk_spans_are_identical_through_seed_and_tool(tmp_path,monkeypatch):
    source='@decorate\ndef gigantic():\n'+'    value = 1\n'*140+'    def nested():\n        return 42\n    return nested()\n'
    ctx=fixture_context(tmp_path,source,filename='main.py')
    def forbidden(*args,**kwargs):pytest.fail('Preflight must not parse or rebuild')
    monkeypatch.setattr('ast.parse',forbidden)
    monkeypatch.setattr('pycode.scanner.scan_python_files',forbidden)
    pre=prepare_question('Explain `gigantic`',decision('Explain `gigantic`'),ctx)
    item=pre.initial_evidence.result.items[0]
    assert item.start_line==1 and item.end_line==len(source.splitlines()) and len(item.chunk_ids)>1
    assert '143:     def nested():' in item.snippet
    tool=retrieve_context(ToolContext(tmp_path,retrieval_context=ctx,initial_evidence=pre.initial_evidence),
                          'Explain `gigantic`',max_snippet_chars=1)
    assert tool.ok and tool.data['initial_evidence_reused']
    assert tool.data['items']==pre.initial_evidence.projection()['items']
    assert tool.data['items'][0]['snippet']==item.snippet and not tool.data['items'][0]['snippet_truncated']
