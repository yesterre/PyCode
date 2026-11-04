"""Published import bindings through real HTTP, Worker, Agent and projections."""
from dataclasses import replace
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
from uuid import UUID

import pytest

from backend.app.core.application_trace import ApplicationTraceNormalizer,public_graph_summary,public_retrieval_summary
from backend.app.core.errors import BackendError
from backend.app.integrations.graph import local_graph
from pycode.agent.trace import ToolTrace
from pycode.graph_schema import graph_to_dict,graph_from_dict
from pycode.retriever import retrieve_for_question
from pycode.storage import load_graph,save_graph
from pycode.tools import ToolContext,query_code_graph,retrieve_context
from test_a03_graph_lifecycle import publish,current
from test_artifacts import _workspace,_snapshot
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_b07_worker_isolation import workers,ready
from test_postgresql import postgresql_engine


FIXTURE=Path(__file__).parents[1]/'fixtures/a03/imports'


def schema2(graph):
    data=graph_to_dict(graph);data['schema_version']=2;data.pop('binding_facts')
    data['producer']='pycode-a03a-v1';data['capabilities']=data['capabilities'][:4]
    for site in data['call_sites']:site.pop('binding_chain')
    return graph_from_dict(data)


def test_published_import_calls_reach_fast_path_agent_and_safe_trace(resources,workers,source_repository,monkeypatch):
    r=resources;state=r.context.app.state
    for source in FIXTURE.rglob('*.py'):
        target=source_repository/source.relative_to(FIXTURE)
        target.parent.mkdir(parents=True,exist_ok=True);target.write_text(source.read_text(encoding='utf-8'),encoding='utf-8')
    monkeypatch.setenv('PYCODE_RETRIEVAL_MODE','hybrid')
    ready(r,workers);snapshot=current(r)
    bundle=state.artifacts.read_ready(r.own,snapshot);before=bundle.graph_path.read_bytes()
    assert load_graph(bundle.graph_path).schema_version==3
    observed=[];ask=state.adapter.engine.ask
    def inspect_ask(*args,**kwargs):
        result=ask(*args,**kwargs);observed.append((kwargs['retrieval_context'],result));return result
    monkeypatch.setattr(state.adapter.engine,'ask',inspect_ask)
    response=r.context.client.post(f'/api/v1/projects/{r.own}/ask',headers=r.headers,json={'question':'run'})
    assert response.status_code==200,response.text
    context,answer=observed[0]
    assert response.json()['evidence']==answer.evidence
    tool=retrieve_context(ToolContext(context.root,retrieval_context=context),'run')
    assert tool.ok and tool.data['evidence']==answer.evidence
    assert any(i.path=='pkg/api.py' and i.symbol=='serve' and any(score.source=='graph' for score in i.source_scores) for i in answer.retrieval.items)
    assert any(s['basis']=='import_binding_chain' for i in answer.retrieval.items for s in i.graph_path)
    graph_response=r.context.client.get(f'/api/v1/projects/{r.own}/snapshots/{snapshot.id}/graph',headers=r.headers,params={'q':'run'})
    body=graph_response.json();assert graph_response.status_code==200 and body['graph_schema_version']==3
    assert any(e['type']=='references' for e in body['edges'])
    assert any(s['binding_declarations'] for s in body['call_sites'])
    assert 'binding_facts' not in body and all('expression' not in s and 'binding_chain' not in s for s in body['call_sites'])
    graph_tool=query_code_graph(ToolContext(context.root,retrieval_context=context),'callees','func:client.py:run')
    trace=ToolTrace(1,'query_graph',datetime.now(timezone.utc));trace.finish(graph_tool)
    _,_,_,payload=ApplicationTraceNormalizer._tool_finished(trace)
    assert payload['graph']['graph_schema_version']==3 and 'import_bindings' in payload['graph']['graph_capabilities']
    assert public_graph_summary(payload['graph'])==payload['graph'] and 'binding_facts' not in payload['graph']
    summary=public_retrieval_summary(answer.retrieval.summary())
    assert summary and summary['graph_schema_version']==3
    assert any(ref.get('relation_resolution')=='static' for ref in summary['references'])
    accepted=r.context.client.post('/api/v1/agent-runs',headers=r.headers,json={'project_id':str(r.own),'task':'run'})
    assert accepted.status_code==202,accepted.text
    task_id=accepted.json()['task_id'];run_id=UUID(accepted.json()['run_id']);root=state.workspace.path_for(r.own)
    (root/'pkg/lib.py').write_text('def work():\n    return 999\nclass Service:\n    pass\n',encoding='utf-8')
    with r.context.uow_factory() as uow:
        with uow.transaction():newer=uow.snapshots.start(r.own,'b'*40)
        new_bundle=publish(state.artifacts,state.adapter,r.own,newer.id,newer.commit_sha,root)
        with uow.transaction():
            uow.snapshots.mark_ready(newer.id,index_artifact_path=new_bundle.index_artifact_path,
                graph_artifact_path=new_bundle.graph_artifact_path,summary=new_bundle.summary)
            uow.projects.set_current_snapshot(r.own,newer.id)
    seen=[];run=state.adapter.engine.run_agent
    def inspect_run(*args,**kwargs):
        fixed=kwargs['retrieval_context'];assert fixed.graph.schema_version==3
        assert fixed.graph.binding['snapshot_id']==str(snapshot.id) and fixed.binding.commit_sha==snapshot.commit_sha
        assert 'return 999' not in dict(fixed.sources)['pkg/lib.py']
        result=query_code_graph(ToolContext(fixed.root,retrieval_context=fixed),'callees','func:client.py:run')
        assert result.ok and any(e['target']=='func:pkg/api.py:serve' for e in result.data['edges'])
        assert not query_code_graph(ToolContext(fixed.root,retrieval_context=fixed),'entry',graph_path='other.json').ok
        seen.append(fixed);return run(*args,**kwargs)
    monkeypatch.setattr(state.adapter.engine,'run_agent',inspect_run)
    workers.agent(task_id);workers.agent(task_id)
    assert len(seen)==1 and bundle.graph_path.read_bytes()==before
    with r.context.uow_factory() as uow,uow.transaction():
        record=uow.agent_runs.get(run_id);assert record.status=='completed' and record.snapshot_id==snapshot.id
    summaries=[e.payload['retrieval'] for rid,e in workers.published if rid==run_id and 'retrieval' in e.payload]
    assert summaries and all(s['graph_schema_version']==3 and s['binding']['snapshot_id']==str(snapshot.id) for s in summaries)
    def forbidden(*args,**kwargs):pytest.fail('Unauthorized request reached Graph Artifact')
    monkeypatch.setattr(state.artifacts,'read_ready',forbidden)
    denied=r.context.client.get(f'/api/v1/projects/{r.own}/snapshots/{snapshot.id}/graph',headers=r.other_headers,params={'q':'run'})
    assert denied.status_code==404


def test_schema2_ready_bundle_keeps_its_original_capability_and_bytes(tmp_path):
    from backend.app.infrastructure.artifacts import SnapshotArtifactStore
    from backend.app.integrations.pycode import PyCodeAdapter
    root,pid,sid,repo=_workspace(tmp_path);store=SnapshotArtifactStore(root);adapter=PyCodeAdapter()
    # A normal older producer: only same-module structure, same Manifest2.
    for source in repo.glob('*.py'):
        source.write_text('def helper():\n    pass\ndef main():\n    helper()\n',encoding='utf-8')
    def older(index,graph):
        result=adapter.index(repo,index_path=index,graph_path=graph,project_id=pid,snapshot_id=sid,commit_sha='a'*40)
        save_graph(schema2(load_graph(graph)),graph)
        return result
    bundle=store.build_and_publish(pid,sid,'a'*40,older,require_chunks=True)
    snapshot=_snapshot(pid,sid,'a'*40,bundle);before=bundle.graph_path.read_bytes()
    read=store.read_ready(pid,snapshot)
    assert load_graph(read.graph_path).schema_version==2 and read.graph_path.read_bytes()==before
    body=local_graph(load_graph(read.graph_path),sid,q='main',node_type=None,file_path=None,limit=100)
    assert body['graph_schema_version']==2 and 'import_bindings' not in body['graph_capabilities']
    assert json.loads((read.graph_path.parent/'manifest.json').read_text())['version']==2


def test_ready_schema2_snapshot_is_not_implicitly_rebuilt_on_index(resources,workers,source_repository,monkeypatch):
    r=resources;state=r.context.app.state
    (source_repository/'main.py').write_text('def helper():\n    pass\ndef main():\n    helper()\n',encoding='utf-8')
    graph=state.adapter.engine.graph
    def older(*args,**kwargs):
        result=schema2(graph(*args,**kwargs));save_graph(result,kwargs['output_path']);return result
    with monkeypatch.context() as patch:
        patch.setattr(state.adapter.engine,'graph',older)
        ready(r,workers)
    snapshot=current(r);bundle=state.artifacts.read_ready(r.own,snapshot);before=bundle.graph_path.read_bytes()
    assert load_graph(bundle.graph_path).schema_version==2
    def forbidden(*args,**kwargs):pytest.fail('Ready Schema2 Snapshot must remain valid without rebuilding')
    monkeypatch.setattr(state.adapter.engine,'build_index',forbidden)
    accepted=r.context.client.post(f'/api/v1/projects/{r.own}/index',headers=r.headers)
    workers.index(accepted.json()['task_id'])
    assert current(r).id==snapshot.id and bundle.graph_path.read_bytes()==before
    response=r.context.client.get(f'/api/v1/projects/{r.own}/snapshots/{snapshot.id}/graph',headers=r.headers,params={'q':'main'})
    assert response.status_code==200 and response.json()['graph_schema_version']==2
    assert 'import_bindings' not in response.json()['graph_capabilities']
