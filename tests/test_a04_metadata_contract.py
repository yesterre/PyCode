"""No credentials, source execution, or model calls: bounded Run metadata."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from backend.app.core.errors import BackendError
from backend.app.core.models import AgentRun,AgentRunStatus
from backend.app.core.run_metadata import (legacy_request,legacy_execution,result_for,
    checked,public_run,validated,LIMITS)


@pytest.fixture
def record():
    snapshot=SimpleNamespace(project_id=uuid4(),id=uuid4(),commit_sha='a'*40)
    req=legacy_request(snapshot,'ask');now=datetime.now(timezone.utc)
    run=AgentRun(uuid4(),snapshot.project_id,snapshot.id,'private task','ask',AgentRunStatus.RUNNING,
                 None,None,None,None,None,now,None,now,
                 request_metadata=req,execution_metadata=legacy_execution(req))
    return snapshot,run


def retrieval(run,count=1):
    return dict(strategy='general',binding=deepcopy(run.request_metadata['binding']),status='ok',
        duration_ms=1,evidence_count=count,diagnostics=[],references=[dict(path=f'p{i}.py',symbol='work',
            start_line=1,end_line=3,chunk_ids=['b'*64],source='keyword',coverage='complete_symbol',
            snippet='DO NOT STORE SOURCE',reason='private text') for i in range(count)])


def test_complete_contract_and_old_null_are_distinct(record):
    snapshot,run=record;meta=result_for(run,[retrieval(run)],intent='general')
    assert meta['quality']=='evidence_available' and meta['outcome']=='completed'
    assert 'DO NOT STORE' not in str(meta) and 'private text' not in str(meta) and 'snippet' not in str(meta)
    done=replace(run,status=AgentRunStatus.COMPLETED,result='answer',result_metadata=meta)
    assert public_run(done,snapshot)==done
    old=replace(done,request_metadata=None,execution_metadata=None,result_metadata=None)
    assert public_run(old,None)==old and result_for(old,[retrieval(run)]) is None


@pytest.mark.parametrize('field,value',[
    ('schema_version',2),('schema_version',True),('schema_version','1'),
    ('prompt','private prompt'),('binding',{}),('entrypoint','question'),
])
def test_request_rejects_unknown_version_types_and_fields_without_echo(record,field,value):
    _,run=record;request=deepcopy(run.request_metadata);request[field]=value
    with pytest.raises(BackendError) as exc:validated('request_metadata',request)
    assert exc.value.code=='invalid_run_metadata' and 'private prompt' not in str(exc.value)


@pytest.mark.parametrize('name',list(LIMITS))
def test_limits_are_utf8_bytes_and_all_unknown_fields_rejected(record,name):
    _,run=record;value=run.request_metadata if name=='request_metadata' else run.execution_metadata if name=='execution_metadata' else result_for(run)
    value=deepcopy(value);value['private']='密'*(LIMITS[name]//2)
    with pytest.raises(BackendError):validated(name,value)


@pytest.mark.parametrize('damage',['project','snapshot','commit','mode','outcome','missing_request','missing_execution','premature_result'])
def test_cross_identity_and_state_contracts_fail_closed(record,damage):
    snapshot,run=record;req=deepcopy(run.request_metadata);exe=deepcopy(run.execution_metadata)
    res=result_for(run);state='completed'
    if damage in {'project','snapshot','commit'}:
        req['binding'][{'project':'project_id','snapshot':'snapshot_id','commit':'commit_sha'}[damage]]= 'c'*40 if damage=='commit' else str(uuid4())
    elif damage=='mode':exe['strategy']='agent'
    elif damage=='outcome':res['outcome']='failed'
    elif damage=='missing_request':req=None
    elif damage=='missing_execution':exe=None
    else:state='running'
    with pytest.raises(BackendError):checked(run.project_id,run.snapshot_id,run.run_type,state,snapshot,req,exe,res)


@pytest.mark.parametrize('damage',['version','private','commit','range','path','diagnostic'])
def test_stored_bad_metadata_is_hidden_without_mutating_historical_text(record,damage):
    snapshot,run=record;res=result_for(run,[retrieval(run)]);bad=deepcopy(res)
    if damage=='version':bad['schema_version']=9
    elif damage=='private':bad['prompt']='SECRET'
    elif damage=='commit':bad['binding']['commit_sha']='c'*40
    elif damage=='range':bad['references'][0]['end_line']=0
    elif damage=='path':bad['references'][0]['path']='../outside.py'
    else:bad['diagnostics']=['SECRET']
    done=replace(run,status=AgentRunStatus.COMPLETED,result='original text',result_metadata=bad)
    public=public_run(done,snapshot)
    assert public.result=='original text' and public.result_metadata is None
    assert public.metadata_warnings==('run_metadata_unavailable',) and done.result_metadata==bad


def test_projection_checks_binding_bounds_refs_and_does_not_infer_answer(record):
    _,run=record;raw=retrieval(run,20);raw['references'][0]['chunk_ids']=['b'*64]*20
    meta=result_for(run,[raw,raw])
    assert len(meta['references'])<=8 and meta['references_omitted']>0
    assert meta['references'][0]['chunk_ids_omitted']==16 and meta['quality']=='partial'
    assert 'chunk_ids_omitted' in meta['diagnostics'] and 'evidence_omitted' in meta['diagnostics']
    foreign=retrieval(run);foreign['binding']['snapshot_id']=str(uuid4())
    denied=result_for(run,[foreign])
    assert denied['references']==[] and denied['quality']=='unavailable'
    assert 'evidence_binding_mismatch' in denied['diagnostics']
    empty=result_for(run,[retrieval(run,0)])
    assert empty['quality']=='insufficient'
    failed=result_for(run,[retrieval(run)],failed=True)
    assert failed['outcome']=='failed' and failed['quality']=='partial' and failed['references']


@pytest.mark.parametrize('raw',[{}, {'strategy':[]}, {'strategy':'general','status':[]}])
def test_malformed_retrieval_is_diagnosed_without_breaking_old_answer(record,raw):
    _,run=record
    metadata=result_for(run,[raw])
    assert metadata['references']==[] and metadata['quality']=='unavailable'
    assert 'evidence_invalid' in metadata['diagnostics']


def test_utf8_budget_omits_whole_references_and_records_the_loss(record):
    import json
    _,run=record;summaries=[]
    for i in range(3):
        raw=retrieval(run,6)
        for j,ref in enumerate(raw['references']):
            ref.update(path='密'*490+f'{i}{j}.py',symbol='符'*190,chunk_ids=['b'*64]*4)
        summaries.append(raw)
    metadata=result_for(run,summaries)
    assert len(json.dumps(metadata,ensure_ascii=False).encode('utf-8'))<=16384
    assert metadata['references_omitted']>0 and 'evidence_omitted' in metadata['diagnostics']
    assert all(ref['path'].startswith('密'*490) and ref['symbol']=='符'*190 for ref in metadata['references'])


@pytest.mark.parametrize('quality',['evidence_available','partial'])
def test_empty_reference_list_cannot_claim_supported_evidence(record,quality):
    _,run=record;metadata=result_for(run);metadata['quality']=quality
    with pytest.raises(BackendError):validated('result_metadata',metadata)
