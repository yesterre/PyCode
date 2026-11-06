"""A05c actual-render budgets, atomic evidence, provenance and deterministic validation."""
from copy import deepcopy
from dataclasses import asdict, replace
import json

import pytest

from pycode.agent.budget import BudgetedLLM, ExecutionBudget, ExecutionLimits, ExecutionStopped
from pycode.agent.context import ContextAssembler
from pycode.agent.context_budget import estimate,public_context_report,public_verification
from pycode.agent.evidence import inspect_context_evidence,verify_context_answer
from pycode.agent.llm_planner import build_llm_planner_prompt,build_llm_next_action_prompt
from pycode.agent.memory import MemoryItem
from pycode.agent.prompts import build_agent_summary_context,render_agent_prompt
from pycode.agent.runtime import run_agent_runtime
from pycode.agent.types import AgentTask,AgentStep,AgentTurn,ToolCall,RuntimeConfig
from pycode.evidence_pack import tool_items
from pycode.question import InitialEvidence
from pycode.retrieval_evidence import Query,RetrievalRequest
from pycode.retriever import retrieve
from pycode.tools import ToolContext,ToolSpec,retrieve_context
from pycode.tools.query_graph import query_code_graph as query_graph
from pycode.tools.base import success
from pycode.tools.read_file import read_file
from test_a02a_retrieval import fixture_context


def bound_budget(context,**changes):
    budget=ExecutionBudget(ExecutionLimits(**changes),binding=context.binding)
    budget.retrieval_context=context
    return budget


def test_all_actual_observations_render_without_plan_position_identity(tmp_path):
    results=[success(name,'Observed',payload='OBSERVED_'+name) for name in ('one','two','three')]
    context=build_agent_summary_context(AgentTask('Inspect',tmp_path),[AgentStep('not_the_tool')],results,load_tasks=False)
    prompt=render_agent_prompt(context)
    assert all('OBSERVED_'+name in prompt for name in ('one','two','three'))
    assert '## Step 2: two' in prompt and '## Step 3: three' in prompt
    assert 'Unbound observation' in prompt


def test_trusted_todo_identity_supplies_purpose_only_for_actual_bound_observation(tmp_path):
    task=AgentTask('Inspect',tmp_path);step=AgentStep('read_file',reason='TRUSTED_PURPOSE',todo_id='todo-1')
    result=success('search_code','Found',matches=[])
    turn=AgentTurn(1,tool_call=ToolCall('search_code',{},planned_todo_id=None),tool_result=result)
    context=build_agent_summary_context(task,[step],[result],turns=[turn],load_tasks=False)
    prompt=render_agent_prompt(context)
    section=next(s.content for s in context.sections if s.name=='tool_results')
    assert 'TRUSTED_PURPOSE' not in section and 'Todo: unbound' in section


@pytest.mark.parametrize('payload',['X'*8000,('汉"\n'*3000)],ids=['ascii','unicode_escaped'])
def test_next_action_contains_one_copy_and_measures_json_escaping(tmp_path,payload):
    task=AgentTask('Inspect',tmp_path);result=success('custom','Observed',payload=payload)
    spec=ToolSpec('custom',lambda c:result)
    budget=ExecutionBudget(ExecutionLimits(context_tokens=131072))
    with budget.activate():
        context=build_agent_summary_context(task,[],[result],tools={'custom':spec},load_tasks=False)
        prompt=build_llm_next_action_prompt(task,agent_context=context,tools={'custom':spec},turn_index=2,
            max_turns=8,steps=[],turns=[],tool_results=[result],todos=[])
    actual=json.loads(prompt.split('Next-action input:\n')[-1])
    assert 'payload' not in actual['recent_tool_results'][0]['data']
    body=next(s['content'] for s in actual['context_sections'] if s['name']=='tool_results')
    assert json.loads(body.split('Data:\n')[-1])['payload']==payload
    assert body.count(json.dumps(payload,ensure_ascii=False)[1:-1])==1
    assert context.report['estimated_tokens']==estimate(prompt)<=budget.limits.context_tokens
    assert public_context_report(context.report)==context.report


def test_context_limit_omits_whole_output_with_source_decision_and_retains_original(tmp_path):
    payload='SENTINEL_'+('汉"\n'*3000)
    result=success('custom','Observed',payload=payload)
    budget=ExecutionBudget(ExecutionLimits(context_tokens=8192))
    with budget.activate():
        context=build_agent_summary_context(AgentTask('Inspect',tmp_path),[],[result],tools={},load_tasks=False)
        prompt=render_agent_prompt(context)
    assert 'SENTINEL_' not in prompt and result.data['payload']==payload
    assert context.report['reason_counts']['context_budget']>=1
    assert context.report['decisions'] and context.report['decisions'][0]['reference']
    assert estimate(prompt)<=8192 and 'context_budget' in prompt
    assert 'No tools were executed.' not in prompt


@pytest.mark.parametrize('tool_cap,kept',[(2048,False),(32768,True)])
def test_long_decorated_multi_chunk_definition_is_whole_or_explicitly_omitted(tmp_path,tool_cap,kept,monkeypatch):
    source='@decorate\ndef lengthy():\n'+''.join('    value = '+repr('x'*40)+'\n' for _ in range(180))+'    return value\n'
    trusted=fixture_context(tmp_path,source)
    result=retrieve_context(ToolContext(tmp_path,retrieval_context=trusted),'lengthy')
    original=deepcopy(result.data)
    def forbidden(*a,**k):pytest.fail('Context construction reparsed source')
    monkeypatch.setattr('ast.parse',forbidden)
    with bound_budget(trusted,tool_output_chars=tool_cap).activate():
        context=build_agent_summary_context(AgentTask('lengthy',tmp_path),[],[result],load_tasks=False)
        prompt=render_agent_prompt(context)
    assert result.data==original and len(original['items'][0]['chunk_ids'])>1
    snippet=original['items'][0]['snippet']
    if kept:
        assert json.dumps(snippet,ensure_ascii=False)[1:-1] in prompt
        ref=context.report['references'][0]
        assert ref['start_line']==1 and ref['end_line']==183 and ref['coverage']=='complete_symbol'
    else:
        assert 'value =' not in prompt
        assert context.report['reason_counts']['tool_output_limit']==1
        ref=context.report['decisions'][0]['reference']
        assert ref['start_line']==1 and ref['end_line']==183


def test_duplicate_code_observations_keep_latest_with_traceable_old_reference(tmp_path):
    trusted=fixture_context(tmp_path,'def signal():\n    return 1\n')
    result=retrieve_context(ToolContext(tmp_path,retrieval_context=trusted),'signal')
    with bound_budget(trusted).activate():
        context=build_agent_summary_context(AgentTask('signal',tmp_path),[],[result,deepcopy(result)],load_tasks=False)
        prompt=render_agent_prompt(context)
    assert context.report['reason_counts']['duplicate']==1
    assert context.report['references'][0]['turn']==2
    assert context.report['decisions'][0]['reference']['turn']==1
    assert prompt.count('2:     return 1')==1


def test_initial_pack_identity_and_original_selection_are_not_rewritten(tmp_path):
    trusted=fixture_context(tmp_path,'def signal():\n    return 1\n')
    retrieval=retrieve(RetrievalRequest(Query('signal')),trusted)
    seed=InitialEvidence.issue(trusted,retrieval,None);before=seed.projection()
    task=AgentTask('signal',tmp_path,initial_evidence=seed)
    with bound_budget(trusted,tool_output_chars=256).activate():
        prompt=build_llm_planner_prompt(task,tools={})
    payload=json.loads(prompt.split('Planning input:\n')[-1])['initial_evidence']
    assert seed.projection()==before and seed.digest==payload['initial_pack_digest']
    assert payload['context_selection']['partial'] and payload['context_selection']['selected_ids']==[]
    assert payload['retrieval']['evidence_count']==len(retrieval.items)
    assert payload['items']==[]


@pytest.mark.parametrize('matching_digest',[True,False])
def test_thin_next_action_result_preserves_only_verified_initial_pack_provenance(tmp_path,matching_digest):
    trusted=fixture_context(tmp_path,'def signal():\n    return 1\n')
    seed=InitialEvidence.issue(trusted,retrieve(RetrievalRequest(Query('signal')),trusted),None)
    task=AgentTask('signal',tmp_path,initial_evidence=seed)
    result=retrieve_context(ToolContext(tmp_path,retrieval_context=trusted,initial_evidence=seed),'signal')
    assert result.data['initial_evidence_reused'] is True
    if not matching_digest:result.data['initial_pack_digest']='f'*64
    before=deepcopy(result.data);original=seed.projection()
    with bound_budget(trusted).activate():
        context=build_agent_summary_context(task,[],[result],tools={},load_tasks=False)
        prompt=build_llm_next_action_prompt(task,agent_context=context,tools={},turn_index=2,
            max_turns=8,steps=[],turns=[],tool_results=[result],todos=[])
    data=json.loads(prompt.split('Next-action input:\n')[-1])['recent_tool_results'][0]['data']
    expected=dict(context_reference='context_sections.tool_results',observation=1)
    if matching_digest:expected.update(initial_evidence_reused=True,initial_pack_digest=seed.digest)
    assert data==expected and 'items' not in data and 'snippet' not in data
    assert result.data==before and seed.projection()==original


@pytest.mark.parametrize('change,warning',[
    (lambda item:item.update(snapshot_id='another'), 'binding_mismatch'),
    (lambda item:item.update(project_id='another'), 'binding_mismatch'),
    (lambda item:item.update(commit_sha='b'*40), 'binding_mismatch'),
    (lambda item:item.update(end_line=999), 'reference_invalid'),
    (lambda item:item.update(path='../outside.py'), 'reference_invalid'),
    (lambda item:item.update(snippet='forged code'), 'source_conflict'),
    (lambda item:item.update(chunk_ids=['f'*64]), 'reference_invalid'),
    (lambda item:item.update(chunk_ids=[{}]), 'reference_invalid'),
    (lambda item:item.update(symbol='forged'), 'definition_missing'),
])
def test_bad_snapshot_or_source_evidence_is_rejected_before_model(tmp_path,change,warning):
    trusted=fixture_context(tmp_path,'def signal():\n    return 1\n')
    result=retrieve_context(ToolContext(tmp_path,retrieval_context=trusted),'signal')
    change(result.data['items'][0])
    budget=bound_budget(trusted)
    with budget.activate(),pytest.raises(ExecutionStopped) as stopped:
        context=build_agent_summary_context(AgentTask('signal',tmp_path),[],[result],load_tasks=False)
        render_agent_prompt(context)
    assert stopped.value.reason=='evidence_invalid' and warning in budget.verification_warnings
    assert budget.usage['model_calls']==0


@pytest.mark.parametrize('phase',['plan','next_action','summary','provider_guard'])
def test_minimum_oversized_task_never_calls_provider(tmp_path,phase):
    task=AgentTask('汉'*6000,tmp_path);calls=[]
    class Model:
        def generate(self,prompt):calls.append(prompt);return 'unused'
    budget=ExecutionBudget(ExecutionLimits(context_tokens=4096))
    with budget.activate(),pytest.raises(ExecutionStopped) as stopped:
        if phase=='plan':build_llm_planner_prompt(task,tools={})
        elif phase=='provider_guard':BudgetedLLM(Model(),budget).generate(task.description)
        else:
            context=build_agent_summary_context(task,[],[],load_tasks=False)
            if phase=='summary':render_agent_prompt(context)
            else:build_llm_next_action_prompt(task,agent_context=context,tools={},turn_index=1,max_turns=8,
                steps=[],turns=[],tool_results=[],todos=[])
    assert stopped.value.reason=='budget_context' and not calls and budget.usage['model_calls']==0
    assert budget.context_reports[-1]['estimated_tokens']>4096
    assert not budget.context_reports[-1]['delivered_to_model']


def test_memory_is_whole_hint_and_cannot_evict_current_code(tmp_path):
    trusted=fixture_context(tmp_path,'def signal():\n    return 1\n')
    result=retrieve_context(ToolContext(tmp_path,retrieval_context=trusted),'signal')
    memory=MemoryItem(id='stale',type='project',title='Old',summary='Old',body='STALE_FACT'*10000)
    with bound_budget(trusted,context_tokens=8192).activate():
        context=build_agent_summary_context(AgentTask('signal',tmp_path),[],[result],
            relevant_memories=[memory],tools={},load_tasks=False)
        prompt=render_agent_prompt(context)
    assert 'STALE_FACT' not in prompt and 'return 1' in prompt
    assert any(d['reference'].get('source')=='memory_hint' for d in context.report['decisions'])
    assert memory.body=='STALE_FACT'*10000


def test_explicit_citation_checks_and_static_graph_are_not_natural_language_proof(tmp_path):
    trusted=fixture_context(tmp_path,'def signal():\n    return 1\n')
    target=next(node.id for node in trusted.graph.nodes if node.type=='function')
    data=query_graph(ToolContext(tmp_path,retrieval_context=trusted),'definitions',target=target).data
    warnings,bad=inspect_context_evidence('query_graph',data,trusted)
    assert not bad and 'relation_unknown' in warnings
    refs=[dict(source='snapshot_source',path='logic.py',start_line=1,end_line=2)]
    checked=verify_context_answer('logic.py:1-2',None,references=refs)
    assert checked['status']=='checked' and checked['natural_language_verified'] is False
    invalid=verify_context_answer('other.py:1',None,references=refs)
    assert 'citation_invalid' in invalid['warnings'] and public_verification(invalid)==invalid
    data['nodes'][0]['path']='other.py'
    assert inspect_context_evidence('query_graph',data,trusted)[1]


def test_actual_provider_prompts_are_capped_and_direct_answer_uses_decision_selection(tmp_path):
    responses=iter([json.dumps([dict(tool='retrieve_context',arguments={},required=False)]),
        json.dumps(dict(action_type='tool_call',tool_name='retrieve_context',arguments={})),
        json.dumps(dict(action_type='final_answer',final_answer='ordinary answer'))])
    class Model:
        def __init__(self):self.prompts=[]
        def generate(self,prompt):self.prompts.append(prompt);assert estimate(prompt)<=8192;return next(responses)
    model=Model()
    result=run_agent_runtime(AgentTask('Inspect',tmp_path),
        RuntimeConfig(enable_memory=False,execution_limits=ExecutionLimits(context_tokens=8192)),llm_client=model,
        tools={'retrieve_context':ToolSpec('retrieve_context',lambda c:success('retrieve_context','Observed',content='X'*20000))})
    assert result.ok and len(model.prompts)==3
    assert result.context_report['phase']=='next_action' and result.context_report['delivered_to_model']
    assert result.context_report['reason_counts']['context_budget']>=1
    assert result.tool_results[0].data['content']=='X'*20000


def test_public_reports_reject_unknown_fields_versions_and_private_text(tmp_path):
    context=build_agent_summary_context(AgentTask('Inspect',tmp_path),[],[],load_tasks=False)
    render_agent_prompt(context);report=context.report
    assert public_context_report(report)==report
    for change in (dict(schema_version=2),dict(prompt='PRIVATE'),dict(estimated_tokens=True),dict(delivered_to_model=1)):
        assert public_context_report(dict(report,**change)) is None
    verdict=verify_context_answer(None,report)
    assert public_verification(dict(verdict,natural_language_verified=True)) is None


@pytest.mark.parametrize('prefix',['Select project memories relevant','Extract durable PyCode memories'])
def test_oversized_optional_memory_input_does_not_block_current_code_or_call_provider(prefix):
    from pycode.llm_client import LLMError
    calls=[]
    class Model:
        def generate(self,prompt):calls.append(prompt);return 'current code'
    budget=ExecutionBudget(ExecutionLimits(context_tokens=4096))
    client=BudgetedLLM(Model(),budget)
    with budget.activate():
        with pytest.raises(LLMError,match='Optional memory input omitted'):
            client.generate(prefix+'X'*10000)
        assert not calls and budget.stop_reason is None and budget.usage['model_calls']==0
        assert client.generate('current code')=='current code'
    assert calls==['current code'] and budget.usage['model_calls']==1


def test_partial_read_has_actual_visible_range_and_original_result_is_unchanged(tmp_path):
    trusted=fixture_context(tmp_path,'def signal():\n    value=123\n    return value\n')
    result=read_file(ToolContext(tmp_path,retrieval_context=trusted),'logic.py',max_chars=20)
    original=deepcopy(result.data)
    with bound_budget(trusted).activate():
        context=build_agent_summary_context(AgentTask('Inspect',tmp_path),[],[result],load_tasks=False)
        render_agent_prompt(context)
    ref=context.report['references'][0]
    assert ref['coverage']=='partial' and ref['end_line']==2 and ref['chunk_ids']
    assert 'partial_coverage' in context.verification_warnings and result.data==original


def test_nonindexed_readme_remains_an_observation_without_snapshot_source_claim(tmp_path):
    trusted=fixture_context(tmp_path,'def signal():\n    return 1\n')
    (tmp_path/'README.md').write_text('Local project instructions',encoding='utf-8')
    result=read_file(ToolContext(tmp_path,retrieval_context=trusted),'README.md')
    with bound_budget(trusted).activate():
        context=build_agent_summary_context(AgentTask('Inspect README',tmp_path),[],[result],load_tasks=False)
        prompt=render_agent_prompt(context)
    assert 'Local project instructions' in prompt and not context.rejected
    assert context.report['references'][0]['source']=='tool_observation'
    assert 'unbound_source' in context.verification_warnings


def test_empty_file_is_an_explicit_empty_source_without_false_conflict(tmp_path):
    trusted=fixture_context(tmp_path,'')
    result=read_file(ToolContext(tmp_path,retrieval_context=trusted),'logic.py')
    with bound_budget(trusted).activate():
        context=build_agent_summary_context(AgentTask('Inspect empty file',tmp_path),[],[result],load_tasks=False)
        render_agent_prompt(context)
    assert not context.rejected and 'definition_missing' in context.verification_warnings
    assert context.report['references'][0]['coverage']=='unavailable'
