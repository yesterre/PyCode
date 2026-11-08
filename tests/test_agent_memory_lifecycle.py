"""A06b deterministic policy and trust boundaries; all models are offline."""
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from pycode.agent.memory import MemoryStore,MemoryRunInfo,extract_memories,load_relevant_memories
from pycode.agent.memory_record import MemoryScope
from pycode.agent.budget import ExecutionBudget,ExecutionStopped
from pycode.chunks import SnapshotBinding
from pycode.cli import memory_project
from pycode.tools import ToolContext
from pycode.tools.memory_tools import memory
from pycode.agent import memory_policy as policy


NOW=datetime(2026,11,8,tzinfo=timezone.utc)
def scope(root):return MemoryScope(root,SnapshotBinding(str(uuid4()),str(uuid4()),'a'*40),str(uuid4()))
def add(store,**kw):return store.add_memory(**(dict(memory_id='note',memory_type='analysis',
    title='Same target',summary='entry hint',body='First complete assertion')|kw))
def raw(store):return store._scan_items()


def test_full_identity_dedup_keeps_source_and_does_not_grow_suffixes(tmp_path):
    s=MemoryStore(tmp_path);a=add(s);b=add(s)
    assert a.id==b.id and a.to_dict()==b.to_dict() and len(raw(s))==1
    assert s.last_write=='duplicate' and s.reason_counts['duplicate']==1
    assert len(policy.content_key(a))==64


def test_long_common_prefix_and_case_are_not_false_duplicates(tmp_path):
    s=MemoryStore(tmp_path)
    first=add(s,body='Same prefix '*100+'Return Foo')
    second=add(s,body='Same prefix '*100+'Return foo')
    assert first.record_id!=second.record_id and len(raw(s))==2
    assert second.lifecycle['state']=='conflict' and first.body.endswith('Foo')
    assert s.list_memories()==[]  # Neither assertion silently wins.
    assert {m.body[-10:] for m in raw(s)}=={'Return Foo','Return foo'}


def test_near_duplicates_are_bounded_candidates_never_merged(tmp_path):
    s=MemoryStore(tmp_path)
    add(s,title='One',body='alpha beta gamma delta epsilon zeta eta theta iota kappa')
    add(s,title='Two',body='alpha beta gamma delta epsilon zeta eta theta iota kappa lambda')
    assert len(s.list_memories())==2 and 'near_duplicate_candidate' in s.diagnostics


def test_conflict_head_cannot_be_revived_by_another_auto_add(tmp_path):
    s=MemoryStore(tmp_path)
    a=add(s);b=add(s,body='Opposite assertion');c=add(s,body=a.body)
    assert len(raw(s))==3 and b.lifecycle['state']==c.lifecycle['state']=='conflict'
    assert not s.list_memories()
    resolved=s.manage_memory(c.id,'revise',body='Explicitly resolved assertion')
    assert resolved.lifecycle['revision']>c.lifecycle['revision']
    assert [m.id for m in s.list_memories()]==[resolved.id]
    assert all(m.body in {'First complete assertion','Opposite assertion','Explicitly resolved assertion'} for m in raw(s))


def test_revision_expiry_never_resurrects_old_content(tmp_path):
    clock=[NOW];s=MemoryStore(tmp_path,clock=lambda:clock[0]);a=add(s)
    b=s.manage_memory(a.id,'revise',body='New assertion')
    assert [e.id for e in s.list_memories()]==[b.id]
    assert next(m for m in raw(s) if m.id==a.id).body==a.body
    clock[0]+=timedelta(days=8)
    assert not s.list_memories()


@pytest.mark.parametrize('operation',['invalidate','delete'])
def test_explicit_lifecycle_removes_from_views_and_model_cannot_manage(tmp_path,operation):
    s=MemoryStore(tmp_path);a=add(s)
    action=s.manage_memory(a.id,operation)
    assert action.lifecycle['state']=={'delete':'deleted','invalidate':'invalidated'}[operation]
    assert not s.list_memories() and not s.search_memories('entry')
    with pytest.raises(PermissionError):s.load_memory(a.id)
    with ExecutionBudget().activate(),pytest.raises(PermissionError):
        s.manage_memory(action.id,'revise',body='Model overwrite')
    assert not memory(ToolContext(tmp_path),operation='delete',id=a.id).ok


def test_user_context_crosses_snapshot_but_not_project_or_code_authority(tmp_path):
    original=scope(tmp_path)
    s=MemoryStore(tmp_path,scope=original,origin='user_explicit',clock=lambda:NOW)
    note=add(s,memory_type='preference',body='Use pytest')
    assert note.lifecycle['category']=='project_context' and note.lifecycle['expires_at'] is None
    newer=MemoryScope(tmp_path,replace(original.binding,snapshot_id=str(uuid4()),commit_sha='b'*40),str(uuid4()))
    assert MemoryStore(tmp_path,scope=newer,clock=lambda:NOW).load_memory(note.id).record_id==note.record_id
    foreign=scope(tmp_path)
    assert not MemoryStore(tmp_path,scope=foreign,clock=lambda:NOW).list_memories()
    with original.activate():
        model=memory(ToolContext(tmp_path),operation='add',id='model',memory_type='preference',body='Other preference')
    assert model.data['memory']['lifecycle']['scope']=='snapshot'
    assert model.data['memory']['provenance']['origin']=='model_inference'
    assert {e.id for e in MemoryStore(tmp_path,scope=newer,clock=lambda:NOW).list_memories()}=={note.id}


@pytest.mark.parametrize('change',['snapshot','commit','project'])
def test_inference_is_filtered_before_calling_selection_model(tmp_path,change):
    original=scope(tmp_path)
    with original.activate():add(MemoryStore(tmp_path),body='PRIVATE_OLD_FACT')
    changed=replace(original.binding,**{'snapshot_id':str(uuid4())} if change=='snapshot'
                    else {'commit_sha':'b'*40} if change=='commit' else {'project_id':str(uuid4())})
    class Forbidden:
        def generate(self,prompt):pytest.fail('Inapplicable memory reached model')
    info=MemoryRunInfo()
    with MemoryScope(tmp_path,changed,str(uuid4())).activate():
        selected,_=load_relevant_memories(tmp_path,task_description='entry',messages=[],llm_client=Forbidden(),info=info)
    assert selected==[] and info.reason_counts


@pytest.mark.parametrize('secret',['password=FAKE_TEST_VALUE','OPENAI_API_KEY=FAKE_TEST_VALUE',
    'Authorization: Bearer FAKE_ONLY_TOKEN','person@example.com','13812345678',
    '-----BEGIN PRIVATE KEY-----','token=FAKE_TOKEN','{"password":"FAKE_ONLY"}',
    '项目密码：FAKE_ONLY','密钥=FAKE_ONLY','postgresql+psycopg://test:FAKE_ONLY@localhost/unused'])
def test_sensitive_fields_are_rejected_without_persisting_or_echoing_values(tmp_path,secret):
    s=MemoryStore(tmp_path)
    with pytest.raises(ValueError) as failure:add(s,body=secret)
    assert secret not in str(failure.value)
    assert not list(s.memory_dir.glob('*.md'))
    result=memory(ToolContext(tmp_path),operation='add',id='secret',memory_type='analysis',body=secret)
    assert not result.ok and secret not in str(result.error)


def test_expiration_is_checked_before_model_catalog_and_load(tmp_path):
    clock=[NOW];s=MemoryStore(tmp_path,clock=lambda:clock[0]);item=add(s)
    clock[0]+=timedelta(days=7)
    assert not s.list_memories() and not s.read_index_text()
    with pytest.raises(PermissionError):s.load_memory(item.id)
    assert 'expired' in s.reason_counts and (s.memory_dir/item.path).exists()


def test_bounded_extraction_counts_full_data_and_rejects_temporary_secret_and_large(tmp_path):
    proposals=[dict(id='temporary',type='analysis',body='work',purpose='working'),
               dict(id='secret',type='analysis',body='password=FAKE_TEST_VALUE'),
               dict(id='large',type='analysis',body='x'*2049)]
    proposals += [dict(id='note-'+str(n),type='analysis',body='valid '+str(n)) for n in range(10)]
    class Model:
        def generate(self,prompt):return json.dumps(proposals)
    info=MemoryRunInfo()
    created,_=extract_memories(tmp_path,task_description='entry',messages=[],tool_results=[],
        answer='Completed answer',llm_client=Model(),info=info)
    assert len(created)==4 and len(raw(MemoryStore(tmp_path)))==4
    assert info.reason_counts['temporary']==1 and info.reason_counts['capacity']==1
    assert info.reason_counts['sensitive_content']==1
    assert info.reason_counts['proposal_limit']>=5
    assert sum(len(m.body.encode()) for m in created)<=8192
    assert all(m.provenance['origin']=='model_inference' for m in created)


@pytest.mark.parametrize('task,answer,reason',[
    ('entry','','empty_answer'),('password=FAKE_ONLY','Answer','input_sensitive'),
    ('x'*24001,'Answer','input_limit')])
def test_extraction_gate_avoids_unnecessary_provider_calls(tmp_path,task,answer,reason):
    class Model:
        def generate(self,prompt):pytest.fail('Gated extraction called provider')
    info=MemoryRunInfo()
    assert extract_memories(tmp_path,task_description=task,messages=[],tool_results=[],answer=answer,llm_client=Model(),info=info)==([],None)
    assert info.reason_counts=={reason:1} and not (tmp_path/'.pclens/memory').exists()


def test_selection_has_safe_provenance_reasons_and_provider_failure_fallback(tmp_path):
    add(MemoryStore(tmp_path),body='entry details')
    class Failing:
        def generate(self,prompt):raise RuntimeError('PRIVATE_MODEL_FAILURE')
    info=MemoryRunInfo()
    chosen,error=load_relevant_memories(tmp_path,task_description='entry',messages=[],llm_client=Failing(),info=info)
    info.relevant_memories=chosen
    summary=info.public_summary()
    assert chosen and 'RuntimeError' in error and 'PRIVATE_MODEL_FAILURE' not in error
    assert policy.public_summary(summary)==summary
    assert summary['references'][0]['reason']=='keyword_fallback'
    assert 'entry details' not in json.dumps(summary)
    assert policy.public_summary(dict(summary,body='PRIVATE')) is None


def test_cli_explicit_revision_and_delete_use_original_store(tmp_path,capsys):
    a=memory_project(tmp_path,'add',memory_id='setting',memory_type='preference',body='Use pytest')
    b=memory_project(tmp_path,'revise',memory_id=a.id,body='Use targeted pytest')
    assert b.lifecycle['reason']=='explicit_revision'
    deleted=memory_project(tmp_path,'delete',memory_id=b.id)
    assert deleted.lifecycle['state']=='deleted' and not MemoryStore(tmp_path).list_memories()


def test_budget_control_signal_is_not_a_memory_policy_rejection(tmp_path):
    budget=ExecutionBudget()
    with budget.activate():
        budget.stop_reason='execution_lost'
        with pytest.raises(ExecutionStopped):add(MemoryStore(tmp_path))


@pytest.mark.parametrize('operation',['invalidate','delete'])
def test_local_operator_can_retract_bound_agent_data_without_claiming_a_new_snapshot(tmp_path,operation):
    original=scope(tmp_path)
    with original.activate():
        item=add(MemoryStore(tmp_path,origin='model_inference'))
    action=MemoryStore(tmp_path,origin='user_explicit').manage_memory(item.id,operation)
    assert action.provenance['project_id']==original.binding.project_id
    assert action.provenance['snapshot_id']==original.binding.snapshot_id
    assert action.provenance['origin']=='model_inference' and action.provenance['run_id'] is None
    assert action.lifecycle['state'] in {'deleted','invalidated'}
    with original.activate():assert MemoryStore(tmp_path).list_memories()==[]
    assert next(m for m in raw(MemoryStore(tmp_path)) if m.id==item.id).body==item.body


def test_runtime_reports_filter_reason_even_when_no_memory_reaches_selection(tmp_path):
    from pycode.agent.runtime import _prepare_memory_context
    from pycode.agent.types import AgentTask,RuntimeConfig
    original=scope(tmp_path)
    with original.activate():add(MemoryStore(tmp_path))
    changed=MemoryScope(tmp_path,replace(original.binding,snapshot_id=str(uuid4())),str(uuid4()))
    with changed.activate():
        info,index=_prepare_memory_context(AgentTask('entry',tmp_path),[],RuntimeConfig(),
            llm_client=None,trace_recorder=None,load_bodies=True)
    assert index=='' and info.public_summary()['selected_count']==0
    assert info.public_summary()['reason_counts']['snapshot_mismatch']==1


@pytest.mark.parametrize('field',['memory_id','title','summary','tags','related_files'])
def test_secret_admission_covers_metadata_before_slug_normalization(tmp_path,field):
    secret='token=FAKE_METADATA_VALUE'
    value=[secret] if field in {'tags','related_files'} else secret
    s=MemoryStore(tmp_path)
    with pytest.raises(ValueError) as error:add(s,**{field:value})
    assert secret not in str(error.value)
    assert not list(s.memory_dir.glob('*.md'))
