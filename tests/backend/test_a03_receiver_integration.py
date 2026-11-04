"""Nominal receiver facts through existing authorized Snapshot/Worker pathways."""
from dataclasses import replace
from datetime import datetime,timezone
from pathlib import Path
from uuid import UUID

import pytest

from backend.app.core.application_trace import ApplicationTraceNormalizer,public_graph_summary,public_retrieval_summary
from pycode.agent.trace import ToolTrace
from pycode.storage import load_graph,save_graph
from pycode.tools import ToolContext,query_code_graph,retrieve_context
from a03_graph_fixtures import schema3
from test_a03_graph_lifecycle import current,publish
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_b07_worker_isolation import workers,ready
from test_postgresql import postgresql_engine

FIXTURE=Path(__file__).parents[1]/'fixtures/a03/receivers'


def install(root):
    for source in FIXTURE.rglob('*.py'):
        target=root/source.relative_to(FIXTURE);target.parent.mkdir(parents=True,exist_ok=True)
        target.write_text(source.read_text(encoding='utf-8'),encoding='utf-8')


def test_nominal_facts_reach_real_fast_path_agent_and_safe_trace(resources,workers,source_repository,monkeypatch):
    r=resources;state=r.context.app.state;install(source_repository)
    monkeypatch.setenv('PYCODE_RETRIEVAL_MODE','hybrid');ready(r,workers)
    snapshot=current(r);bundle=state.artifacts.read_ready(r.own,snapshot);before=bundle.graph_path.read_bytes()
    observed=[];ask=state.adapter.engine.ask
    def capture(*args,**kwargs):
        result=ask(*args,**kwargs);observed.append((kwargs['retrieval_context'],result));return result
    monkeypatch.setattr(state.adapter.engine,'ask',capture)
    response=r.context.client.post(f'/api/v1/projects/{r.own}/ask',headers=r.headers,json={'question':'run'})
    assert response.status_code==200,response.text
    context,answer=observed[0];assert context.graph.schema_version==4 and response.json()['evidence']==answer.evidence
    tool=retrieve_context(ToolContext(context.root,retrieval_context=context),'run')
    assert tool.ok and tool.data['evidence']==answer.evidence
    assert any(i.symbol=='Service.work' and i.path=='service.py' for i in answer.retrieval.items)
    graph_tool=query_code_graph(ToolContext(context.root,retrieval_context=context),'callees','method:consumer.py:Consumer.run')
    assert any(e['target']=='method:service.py:Service.work' for e in graph_tool.data['edges'])
    assert all('expression' not in s for s in graph_tool.data['call_sites']) and 'flow_facts' not in graph_tool.data
    trace=ToolTrace(1,'query_graph',datetime.now(timezone.utc));trace.finish(graph_tool)
    _,_,_,payload=ApplicationTraceNormalizer._tool_finished(trace)
    assert payload['graph']['graph_schema_version']==4 and 'local_receiver_bindings' in payload['graph']['graph_capabilities']
    assert public_graph_summary(payload['graph'])==payload['graph'] and 'flow_chain' not in payload['graph']
    assert public_retrieval_summary(answer.retrieval.summary())['graph_schema_version']==4
    body=r.context.client.get(f'/api/v1/projects/{r.own}/snapshots/{snapshot.id}/graph',headers=r.headers,params={'q':'Consumer.run'}).json()
    assert body['graph_schema_version']==4 and any(s['flow_evidence'] for s in body['call_sites'])
    assert 'flow_facts' not in body and all('flow_chain' not in s for s in body['call_sites'])
    accepted=r.context.client.post('/api/v1/agent-runs',headers=r.headers,json={'project_id':str(r.own),'task':'run'})
    assert accepted.status_code==202,accepted.text
    task_id=accepted.json()['task_id'];run_id=UUID(accepted.json()['run_id']);root=state.workspace.path_for(r.own)
    (root/'service.py').write_text((root/'service.py').read_text(encoding='utf-8').replace('return 1','return 999'),encoding='utf-8')
    with r.context.uow_factory() as uow:
        with uow.transaction():newer=uow.snapshots.start(r.own,'b'*40)
        new=publish(state.artifacts,state.adapter,r.own,newer.id,newer.commit_sha,root)
        with uow.transaction():
            uow.snapshots.mark_ready(newer.id,index_artifact_path=new.index_artifact_path,graph_artifact_path=new.graph_artifact_path,summary=new.summary)
            uow.projects.set_current_snapshot(r.own,newer.id)
    seen=[];run=state.adapter.engine.run_agent
    def inspect(*args,**kwargs):
        fixed=kwargs['retrieval_context'];assert fixed.binding.snapshot_id==str(snapshot.id)
        assert fixed.graph.binding==context.graph.binding and fixed.graph.schema_version==4
        assert 'return 999' not in dict(fixed.sources)['service.py']
        result=query_code_graph(ToolContext(fixed.root,retrieval_context=fixed),'callees','method:consumer.py:Consumer.run')
        assert result.ok and any(e['target']=='method:service.py:Service.work' for e in result.data['edges'])
        assert not query_code_graph(ToolContext(fixed.root,retrieval_context=fixed),'entry',graph_path='wrong.json').ok
        seen.append(fixed);return run(*args,**kwargs)
    monkeypatch.setattr(state.adapter.engine,'run_agent',inspect)
    workers.agent(task_id);workers.agent(task_id)
    assert len(seen)==1 and bundle.graph_path.read_bytes()==before
    with r.context.uow_factory() as uow,uow.transaction():
        record=uow.agent_runs.get(run_id);assert record.status=='completed' and record.snapshot_id==snapshot.id
    summaries=[e.payload['retrieval'] for rid,e in workers.published if rid==run_id and 'retrieval' in e.payload]
    assert summaries and all(s['graph_schema_version']==4 and s['binding']['snapshot_id']==str(snapshot.id) for s in summaries)
    def forbidden(*args,**kwargs):pytest.fail('Unauthorized request read nominal Graph facts')
    monkeypatch.setattr(state.artifacts,'read_ready',forbidden)
    denied=r.context.client.get(f'/api/v1/projects/{r.own}/snapshots/{snapshot.id}/graph',headers=r.other_headers,params={'q':'Consumer'})
    assert denied.status_code==404


def test_ready_schema3_remains_readable_without_implicit_flow_upgrade(resources,workers,source_repository,monkeypatch):
    r=resources;state=r.context.app.state;install(source_repository);graph=state.adapter.engine.graph
    def older(*args,**kwargs):
        result=schema3(graph(*args,**kwargs));save_graph(result,kwargs['output_path']);return result
    with monkeypatch.context() as patch:
        patch.setattr(state.adapter.engine,'graph',older);ready(r,workers)
    snapshot=current(r);bundle=state.artifacts.read_ready(r.own,snapshot);before=bundle.graph_path.read_bytes()
    assert load_graph(bundle.graph_path).schema_version==3
    assert next(s for s in load_graph(bundle.graph_path).call_sites if s['expression']=='self.helper')['resolution']=='unknown'
    def forbidden(*args,**kwargs):pytest.fail('Old ready Schema3 must not be rebuilt implicitly')
    monkeypatch.setattr(state.adapter.engine,'build_index',forbidden)
    accepted=r.context.client.post(f'/api/v1/projects/{r.own}/index',headers=r.headers);workers.index(accepted.json()['task_id'])
    assert current(r).id==snapshot.id and bundle.graph_path.read_bytes()==before
    body=r.context.client.get(f'/api/v1/projects/{r.own}/snapshots/{snapshot.id}/graph',headers=r.headers,params={'q':'Consumer'}).json()
    assert body['graph_schema_version']==3 and 'local_receiver_bindings' not in body['graph_capabilities']
