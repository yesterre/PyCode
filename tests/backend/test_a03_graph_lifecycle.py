"""A03a real Store/Worker/HTTP/Agent contracts; no model or schema migration."""
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import shutil
from uuid import UUID, uuid4
from types import SimpleNamespace

import pytest

from backend.app.core.application_trace import ApplicationTraceNormalizer, public_graph_summary, public_retrieval_summary
from backend.app.core.errors import BackendError
from backend.app.db.models import BackgroundTaskORM
from backend.app.infrastructure.artifacts import SnapshotArtifactStore
from backend.app.integrations.pycode import PyCodeAdapter
from backend.app.services.stale_reconciliation import StaleRunningReconciler
from pycode.agent.trace import ToolTrace
from pycode.graph_schema import graph_info
from pycode.storage import load_graph
from pycode.tools import ToolContext, query_code_graph
from test_artifacts import _workspace, _snapshot, _publish
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_b07_worker_isolation import workers, ready, mutate
from test_postgresql import postgresql_engine


def publish(store,adapter,project,snapshot,commit,repo):
    return store.build_and_publish(project,snapshot,commit,
        lambda i,g:adapter.index(repo,index_path=i,graph_path=g,project_id=project,snapshot_id=snapshot,commit_sha=commit),
        require_chunks=True)


def current(r):
    with r.context.uow_factory() as uow,uow.transaction():
        project=uow.projects.get(r.own)
        return uow.snapshots.get(project.current_snapshot_id)


def test_v2_graph_uses_existing_manifest_and_failed_publish_rolls_back(tmp_path,monkeypatch):
    root,pid,sid,repo=_workspace(tmp_path)
    store,adapter=SnapshotArtifactStore(root),PyCodeAdapter()
    bundle=publish(store,adapter,pid,sid,'a'*40,repo)
    snapshot=_snapshot(pid,sid,'a'*40,bundle)
    graph=load_graph(bundle.graph_path)
    assert graph.schema_version==3 and graph.binding['snapshot_id']==str(sid)
    assert json.loads((bundle.index_path.parent/'manifest.json').read_text())['version']==2
    before={p.name:p.read_bytes() for p in bundle.index_path.parent.iterdir()}
    original=store._read_bundle;armed=[];failed=[]
    def reject_new(*args):
        if armed and not failed:
            failed.append(True);raise BackendError('artifacts_unavailable','Injected validation failure')
        return original(*args)
    monkeypatch.setattr(store,'_read_bundle',reject_new)
    with pytest.raises(BackendError):
        store.build_and_publish(pid,sid,'a'*40,
            lambda i,g:adapter.index(repo,index_path=i,graph_path=g,project_id=pid,snapshot_id=sid,commit_sha='a'*40),
            require_chunks=True,before_publish=lambda:armed.append(True))
    assert failed and store.read_ready(pid,snapshot).summary==bundle.summary
    assert {p.name:p.read_bytes() for p in bundle.index_path.parent.iterdir()}==before


@pytest.mark.parametrize('field',['project_id','snapshot_id','commit_sha'])
def test_bound_graph_rejects_foreign_identity_even_when_manifest_hash_is_updated(tmp_path,field):
    root,pid,sid,repo=_workspace(tmp_path);store=SnapshotArtifactStore(root)
    bundle=publish(store,PyCodeAdapter(),pid,sid,'a'*40,repo)
    snapshot=_snapshot(pid,sid,'a'*40,bundle)
    data=json.loads(bundle.graph_path.read_text());data['binding'][field]='b'*40 if field=='commit_sha' else str(uuid4())
    bundle.graph_path.write_text(json.dumps(data),encoding='utf-8')
    manifest=bundle.index_path.parent/'manifest.json';value=json.loads(manifest.read_text())
    value['graph_sha256']=hashlib.sha256(bundle.graph_path.read_bytes()).hexdigest();manifest.write_text(json.dumps(value),encoding='utf-8')
    with pytest.raises(BackendError) as error:store.read_ready(pid,snapshot)
    assert error.value.code=='artifacts_unavailable'


def test_recovery_restores_v2_graph_from_valid_backup(tmp_path):
    root,pid,sid,repo=_workspace(tmp_path);store=SnapshotArtifactStore(root)
    bundle=publish(store,PyCodeAdapter(),pid,sid,'a'*40,repo)
    original=bundle.graph_path.read_bytes();directory=bundle.index_path.parent
    backup=directory.parent/f'.{sid}.backup-{uuid4()}'
    shutil.copytree(directory,backup)
    broken=json.loads(bundle.graph_path.read_text());broken['schema_version']=999
    bundle.graph_path.write_text(json.dumps(broken),encoding='utf-8')
    recovered=store.recover_published(pid,sid,'a'*40)
    assert recovered.graph_path.read_bytes()==original and load_graph(recovered.graph_path).schema_version==3


def test_old_graph_read_and_chunk_backfill_preserve_original_base_bytes(tmp_path):
    root,pid,sid,repo=_workspace(tmp_path);store=SnapshotArtifactStore(root);adapter=PyCodeAdapter()
    old=_publish(store,adapter,pid,sid,'a'*40,repo)
    snapshot=_snapshot(pid,sid,'a'*40,old)
    before=(old.index_path.read_bytes(),old.graph_path.read_bytes())
    assert load_graph(old.graph_path).schema_version==1
    def chunks_only(i,g):
        adapter.build_chunks(repo,i.parent/'chunks.json',project_id=pid,snapshot_id=sid,commit_sha='a'*40)
        return old.summary
    upgraded=store.build_and_publish(pid,sid,'a'*40,chunks_only,require_chunks=True,preserve_base=True)
    assert (upgraded.index_path.read_bytes(),upgraded.graph_path.read_bytes())==before
    assert store.read_ready(pid,snapshot).chunks_path and load_graph(upgraded.graph_path).schema_version==1


def test_worker_new_graph_single_parse_api_metadata_and_redelivery(resources,workers,monkeypatch):
    r=resources;state=r.context.app.state;counter=Counter()
    original=state.adapter.engine.build_index
    import pycode.parser
    parse=pycode.parser.ast.parse
    def count(source,filename='<unknown>',**kwargs):
        counter[str(filename)]+=1;return parse(source,filename=filename,**kwargs)
    monkeypatch.setattr(pycode.parser.ast,'parse',count)
    accepted=r.context.client.post(f'/api/v1/projects/{r.own}/index',headers=r.headers)
    assert accepted.status_code==202
    workers.index(accepted.json()['task_id'])
    snapshot=current(r);bundle=state.artifacts.read_ready(r.own,snapshot)
    graph=load_graph(bundle.graph_path)
    assert graph.schema_version==3 and graph.binding['project_id']==str(r.own)
    assert graph.binding['snapshot_id']==str(snapshot.id)
    assert counter and all(v==1 for v in counter.values())
    initial=counter.copy()
    workers.index(accepted.json()['task_id'])
    assert counter==initial
    response=r.context.client.get(f'/api/v1/projects/{r.own}/snapshots/{snapshot.id}/graph',headers=r.headers,params={'file_path':'main.py'})
    assert response.status_code==200,response.text
    body=response.json();assert body['graph_schema_version']==3 and 'call_sites' in body
    assert all('expression' not in c for c in body['call_sites'])
    assert any(n.get('symbol_key') for n in body['nodes'])
    def forbidden(*a,**kw):pytest.fail('Denied request must not read Graph')
    monkeypatch.setattr(state.artifacts,'read_ready',forbidden)
    denied=r.context.client.get(f'/api/v1/projects/{r.own}/snapshots/{snapshot.id}/graph',headers=r.other_headers,params={'q':'main'})
    assert denied.status_code==404


def test_existing_ready_graph_is_not_rebuilt_or_misreported_as_upgraded(resources,workers,monkeypatch):
    r=resources;state=r.context.app.state;original=state.adapter.engine.build_index
    def legacy(*args,**kwargs):
        kwargs['include_graph_metadata']=False
        return original(*args,**kwargs)
    with monkeypatch.context() as patch:
        patch.setattr(state.adapter.engine,'build_index',legacy)
        ready(r,workers)
    snapshot=current(r);bundle=state.artifacts.read_ready(r.own,snapshot);before=bundle.graph_path.read_bytes()
    assert load_graph(bundle.graph_path).schema_version==1 and bundle.chunks_path is not None
    def forbidden(*a,**k):pytest.fail('Ready legacy Graph must not be invisibly upgraded')
    monkeypatch.setattr(state.adapter.engine,'build_index',forbidden)
    accepted=r.context.client.post(f'/api/v1/projects/{r.own}/index',headers=r.headers)
    workers.index(accepted.json()['task_id'])
    assert current(r).id==snapshot.id and bundle.graph_path.read_bytes()==before
    response=r.context.client.get(f'/api/v1/projects/{r.own}/snapshots/{snapshot.id}/graph',headers=r.headers,params={'q':'main'})
    assert response.json()['graph_schema_version']==1
    assert response.json()['graph_warnings']==['legacy_graph_unverified']


def test_worker_recovers_published_v2_after_crash_without_reparse(resources,workers,monkeypatch):
    r=resources;state=r.context.app.state
    accepted=r.context.client.post(f'/api/v1/projects/{r.own}/index',headers=r.headers)
    tid=UUID(accepted.json()['task_id']);original=state.artifacts._publish
    class Crash(BaseException):pass
    def publish_crash(*args,**kwargs):original(*args,**kwargs);raise Crash()
    with monkeypatch.context() as patch:
        patch.setattr(state.artifacts,'_publish',publish_crash)
        with pytest.raises(Crash):workers.index(str(tid))
    with r.context.uow_factory() as uow,uow.transaction():
        task=uow.background_tasks.get(tid)
        assert task.status=='running' and task.snapshot_id is not None
        mutate(uow,'background_tasks',BackgroundTaskORM,tid,lease_expires_at=datetime.now(timezone.utc)-timedelta(minutes=1))
    with r.context.uow_factory() as uow:
        outcome=StaleRunningReconciler(uow,state.project_operations).reconcile()
    assert outcome.requeued==1
    def forbidden(*a,**k):pytest.fail('Recovery must use the already published Graph')
    monkeypatch.setattr(state.adapter,'index',forbidden)
    monkeypatch.setattr(state.workspace,'synchronize',forbidden)
    workers.index(str(tid))
    snapshot=current(r)
    assert load_graph(state.artifacts.read_ready(r.own,snapshot).graph_path).schema_version==3


def test_graph_tool_trace_is_bounded_code_free_and_projection_idempotent(resources,workers):
    r=resources;ready(r,workers);state=r.context.app.state;snapshot=current(r)
    bundle=state.artifacts.read_ready(r.own,snapshot)
    with r.context.uow_factory() as uow:
        from test_b04_project_derived_auth import project_service
        context=project_service(r.context,uow).retrieval_context(state.workspace.path_for(r.own),snapshot,bundle)
    result=query_code_graph(ToolContext(context.root,retrieval_context=context),'calls','func:main.py:main')
    assert result.ok and result.data['graph']['binding']['snapshot_id']==str(snapshot.id)
    trace=ToolTrace(1,'query_graph',datetime.now(timezone.utc));trace.finish(result)
    safe=public_graph_summary({**trace.graph,'api_key':'SECRET','expression':'PRIVATE_CODE'})
    assert safe and safe['graph_schema_version']==3 and public_graph_summary(safe)==safe
    assert 'SECRET' not in json.dumps(safe) and 'PRIVATE_CODE' not in json.dumps(safe)
    _,_,_,payload=ApplicationTraceNormalizer._tool_finished(trace)
    assert payload['graph']==safe and 'call_sites' not in safe
    from backend.app.core.realtime_trace import RealtimeTraceSession
    run=SimpleNamespace(id=uuid4(),started_at=datetime.now(timezone.utc),run_type='agent',snapshot_id=snapshot.id)
    live=RealtimeTraceSession(run,SimpleNamespace(ensure_owned=lambda:None))
    live.observe('tool_finished',trace)
    assert live.events[-1].payload['graph']==safe


@pytest.mark.parametrize('field,value',[('graph_schema_version',True),('graph_capabilities',[{}]),
    ('graph_capabilities',['unknown_capability']),('unknown_count',True),('unknown_count',2),('binding',{'project_id':'invalid'})])
def test_graph_trace_rejects_invalid_metadata_without_exposing_raw_input(field,value):
    data={'graph_schema_version':2,'graph_capabilities':['lexical_scopes'],'binding':None,
          'call_sites_count':1,'unknown_count':1,'ambiguous_count':0}
    assert public_graph_summary({**data,field:value}) is None


def test_agent_worker_keeps_original_v2_graph_after_snapshot_switch_and_redelivery(resources,workers,monkeypatch):
    r=resources;ready(r,workers);state=r.context.app.state;original_snapshot=current(r)
    accepted=r.context.client.post('/api/v1/agent-runs',headers=r.headers,json={'project_id':str(r.own),'task':'return'})
    assert accepted.status_code==202,accepted.text
    task_id=accepted.json()['task_id'];run_id=UUID(accepted.json()['run_id'])
    root=state.workspace.path_for(r.own)
    with r.context.uow_factory() as uow:
        with uow.transaction():newer=uow.snapshots.start(r.own,'b'*40)
        bundle=publish(state.artifacts,state.adapter,r.own,newer.id,newer.commit_sha,root)
        with uow.transaction():
            uow.snapshots.mark_ready(newer.id,index_artifact_path=bundle.index_artifact_path,
                                    graph_artifact_path=bundle.graph_artifact_path,summary=bundle.summary)
            uow.projects.set_current_snapshot(r.own,newer.id)
    observed=[];original=state.adapter.engine.run_agent
    def inspect(*args,**kwargs):
        context=kwargs['retrieval_context']
        assert context.graph.schema_version==3
        assert context.graph.binding['snapshot_id']==str(original_snapshot.id)
        assert context.graph.binding['commit_sha']==original_snapshot.commit_sha
        assert context.binding.snapshot_id==str(original_snapshot.id)
        denied=query_code_graph(ToolContext(context.root,retrieval_context=context),'entry',graph_path='.pclens/code_graph.json')
        assert not denied.ok and denied.error=='trusted_artifacts_cannot_be_overridden'
        observed.append(context)
        return original(*args,**kwargs)
    monkeypatch.setattr(state.adapter.engine,'run_agent',inspect)
    workers.agent(task_id);workers.agent(task_id)
    assert len(observed)==1 and current(r).id==newer.id
    with r.context.uow_factory() as uow,uow.transaction():
        run=uow.agent_runs.get(run_id)
        assert run.status=='completed' and run.snapshot_id==original_snapshot.id
    summaries=[e.payload['retrieval'] for rid,e in workers.published if rid==run_id and 'retrieval' in e.payload]
    assert summaries and all(s['graph_schema_version']==3 and s['binding']['snapshot_id']==str(original_snapshot.id) for s in summaries)
