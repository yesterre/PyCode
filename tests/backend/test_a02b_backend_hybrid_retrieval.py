"""Hybrid/Pack integration through existing HTTP and Worker assembly, both UoWs."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from backend.app.config import RetrievalSettings
from backend.app.core.application_trace import public_retrieval_summary
from backend.app.core.models import AgentRunStatus
from backend.app.core.realtime_trace import RealtimeTraceSession
from backend.app.services.background_tasks import BackgroundTaskService
from pycode.agent.trace import ToolTrace
from pycode.embedding import EmbeddingError
from pycode.tools import ToolContext, retrieve_context
from pycode.tools.base import success
from test_a02b_backend_vector_retrieval import vector_env, snapshot
from test_a01b_semantics import semantic_env
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_postgresql import postgresql_engine


@pytest.fixture
def hybrid_env(vector_env):
    env=vector_env
    env.config=RetrievalSettings('hybrid', token_budget=12000,graph_nodes=8,graph_edge_checks=16)
    env.app.state.retrieval_settings_factory=lambda:env.config
    return env


def test_http_tool_and_prompt_use_one_budgeted_pack(hybrid_env,monkeypatch):
    env=hybrid_env
    seen=[]
    original=env.app.state.adapter.engine.ask
    def ask(*args,**kwargs):
        result=original(*args,**kwargs)
        seen.append((kwargs['retrieval_context'],result.retrieval))
        return result
    monkeypatch.setattr(env.app.state.adapter.engine,'ask',ask)
    response=env.ask()
    assert response.status_code==200,response.text
    summary=response.json()['retrieval']
    assert summary['mode']=='hybrid' and summary['pack']['estimated_tokens']<=env.config.token_budget
    assert summary['graph_stats']['edge_checks']<=16
    assert any(len(r['source_scores'])>=2 for r in summary['references'])
    context,result=seen[0]
    assert response.json()['retrieval']==result.summary()
    tool=retrieve_context(ToolContext(context.root,retrieval_context=context),'return',max_snippet_chars=1)
    assert tool.ok and tool.data['items']==[{**i.to_dict(),'snippet_truncated':False} for i in result.items]
    assert tool.data['retrieval']['pack']==summary['pack']
    assert tool.data['evidence']==response.json()['evidence']
    for i in result.items:
        assert i.snippet in env.app.state.adapter.llm_client.prompts[-1]
    assert all(p.closed for p in env.created)


def test_expansion_and_token_limits_are_used_by_actual_service(hybrid_env):
    env=hybrid_env
    env.config=replace(env.config,token_budget=4096,graph_nodes=2,graph_edge_checks=1)
    response=env.ask()
    assert response.status_code==200,response.text
    summary=response.json()['retrieval']
    assert summary['pack']['estimated_tokens']<=4096
    assert summary['graph_stats']['edge_checks']<=1 and summary['graph_stats']['visited_nodes']<=2
    assert summary['graph_stats']['limited']
    assert summary['pack']['discarded_count']>0
    assert snapshot(env).status=='ready'


def test_hybrid_provider_failure_retains_keyword_graph_and_ready(hybrid_env):
    env=hybrid_env; env.failure=EmbeddingError('embedding_timeout')
    response=env.ask()
    assert response.status_code==200,response.text
    summary=response.json()['retrieval']
    assert summary['status']=='degraded' and 'vector_provider_failed' in summary['diagnostics']
    assert summary['references'] and all(s['source']!='vector' for r in summary['references'] for s in r['source_scores'])
    assert env.created and all(p.closed for p in env.created)
    assert snapshot(env).status=='ready'


def test_trace_projection_is_bounded_code_free_and_idempotent(hybrid_env):
    env=hybrid_env
    response=env.ask(); assert response.status_code==200
    raw={**response.json()['retrieval'],'api_key':'SECRET','query':'PRIVATE_QUERY','vectors':[[1]]}
    safe=public_retrieval_summary(raw)
    assert safe and safe['mode']=='hybrid' and safe['pack']['estimated_tokens']<=12000
    assert 'discarded' not in safe['pack']
    assert all('graph_path' not in r for r in safe['references'])
    assert public_retrieval_summary(safe)==safe
    assert not any(x in json.dumps(safe) for x in ('SECRET','PRIVATE_QUERY','vectors','snippet'))
    trace=ToolTrace(1,'retrieve_context',datetime.now(timezone.utc))
    trace.finish(success('retrieve_context','selected',retrieval=raw))
    with env.r.context.uow_factory() as uow,uow.transaction():
        run=uow.agent_runs.create(env.r.own,env.snapshot_id,task='return',run_type='agent',model=None)
    live=RealtimeTraceSession(replace(run,started_at=datetime.now(timezone.utc)),SimpleNamespace(ensure_owned=lambda:None),None)
    live.observe('tool_finished',trace)
    assert live.events[-1].payload['retrieval']==safe


def test_denied_hybrid_request_cannot_read_vectors_or_call_provider(hybrid_env,monkeypatch):
    env=hybrid_env
    def forbidden(*args,**kwargs):pytest.fail('Authorization must precede retrieval')
    monkeypatch.setattr(env.app.state.artifacts,'read_vector_index',forbidden)
    env.app.state.embedding_provider_factory=forbidden
    response=env.r.context.client.post(f'/api/v1/projects/{env.r.own}/ask',headers=env.r.other_headers,json={'question':'return'})
    assert response.status_code==404 and not env.created


def test_worker_hybrid_pack_keeps_original_snapshot_after_switch_and_redelivery(hybrid_env,monkeypatch):
    env=hybrid_env
    accepted=env.r.context.client.post('/api/v1/agent-runs',headers=env.r.headers,json={'project_id':str(env.r.own),'task':'return'})
    assert accepted.status_code==202,accepted.text
    task_id=UUID(accepted.json()['task_id'])
    root=env.app.state.workspace.path_for(env.r.own)
    with env.r.context.uow_factory() as uow:
        with uow.transaction():newer=uow.snapshots.start(env.r.own,'b'*40)
        bundle=env.app.state.artifacts.build_and_publish(env.r.own,newer.id,newer.commit_sha,
            lambda i,g:env.app.state.adapter.index(root,index_path=i,graph_path=g,project_id=env.r.own,
                                                  snapshot_id=newer.id,commit_sha=newer.commit_sha),require_chunks=True)
        with uow.transaction():
            uow.snapshots.mark_ready(newer.id,index_artifact_path=bundle.index_artifact_path,graph_artifact_path=bundle.graph_artifact_path,summary=bundle.summary)
            uow.projects.set_current_snapshot(env.r.own,newer.id)
    from backend.app.tasks import runtime
    events=[]
    for name,value in {'_adapter':env.app.state.adapter,'_workspace':env.app.state.workspace,'_artifacts':env.app.state.artifacts,
        '_project_operations':env.app.state.project_operations,'_agent_workspace':env.app.state.agent_workspace,'_admission':None,
        '_embedding_settings_factory':env.settings_factory,'_embedding_provider_factory':env.factory,
        '_retrieval_settings_factory':lambda:env.config,'_background_task_service':lambda u:BackgroundTaskService(u),
        '_trace_publisher':SimpleNamespace(publish=lambda rid,e:events.append(e))}.items():monkeypatch.setattr(runtime,name,value)
    with env.r.context.uow_factory() as uow:
        session=getattr(uow,'session',SimpleNamespace(in_transaction=lambda:False,close=lambda:None))
        monkeypatch.setattr(runtime,'_database',SimpleNamespace(open_session=lambda:session))
        monkeypatch.setattr(runtime,'SqlAlchemyUnitOfWork',lambda session:uow)
        with runtime.worker_agent_run_service() as service:
            run=service.execute(task_id); assert run.status==AgentRunStatus.COMPLETED
            count=len(env.created)
            assert service.execute(task_id).id==run.id and len(env.created)==count
    summaries=[e.payload['retrieval'] for e in events if 'retrieval' in e.payload]
    assert summaries and all(s['mode']=='hybrid' and s['binding']['snapshot_id']==str(env.snapshot_id) for s in summaries)
    assert all(s['pack']['estimated_tokens']<=12000 for s in summaries)


def test_hybrid_entry_http_and_onboard_tool_retain_structural_evidence_without_provider(hybrid_env,monkeypatch):
    env=hybrid_env
    env.failure=EmbeddingError('embedding_unavailable')
    contexts=[]
    original=env.app.state.adapter.engine.ask
    def ask(*args,**kwargs):
        contexts.append(kwargs['retrieval_context'])
        return original(*args,**kwargs)
    monkeypatch.setattr(env.app.state.adapter.engine,'ask',ask)
    response=env.ask('return 入口在哪里')
    assert response.status_code==200,response.text
    body=response.json()
    assert body['intent']=='entry'
    assert [r['path'] for r in body['retrieval']['references']]==['main.py']
    assert body['retrieval']['references'][0]['coverage']=='complete_file'
    context=contexts[0]
    tool=retrieve_context(ToolContext(context.root,retrieval_context=context),'生成项目新手阅读顺序',intent='onboard')
    assert tool.ok and [i['path'] for i in tool.data['items']]==['main.py','service.py']
    assert tool.data['retrieval']['binding']['snapshot_id']==str(env.snapshot_id)
    assert tool.data['retrieval']['pack']['estimated_tokens']<=env.config.token_budget
    assert not env.created


def test_hybrid_impact_http_preserves_importer_when_graph_expansion_is_disabled(hybrid_env):
    env=hybrid_env
    env.config=replace(env.config,graph_depth=0,graph_edge_checks=1)
    env.failure=EmbeddingError('embedding_unavailable')
    response=env.r.context.client.post(f'/api/v1/projects/{env.r.own}/impact',headers=env.r.headers,
                                      json={'file_path':'service.py'})
    assert response.status_code==200,response.text
    body=response.json()
    assert [r['path'] for r in body['retrieval']['references']]==['service.py','main.py']
    assert 'file:main.py --imports--> file:service.py' in body['evidence']
    assert body['retrieval']['mode']=='hybrid' and body['retrieval']['pack']
    assert body['retrieval']['binding']['snapshot_id']==str(env.snapshot_id)
    assert snapshot(env).status=='ready' and not env.created
