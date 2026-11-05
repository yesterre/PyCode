"""Unified Worker with published vectors, real assembly and dedicated UoWs."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from sqlalchemy import update

from backend.app.config import RetrievalSettings, StaleReconciliationSettings
from backend.app.db.models import BackgroundTaskORM
from backend.app.runtime import build_agent_run_service
from backend.app.services.background_tasks import BackgroundTaskService
from backend.app.services.stale_reconciliation import StaleRunningReconciler
from test_a02b_backend_vector_retrieval import vector_env, snapshot
from test_a01b_semantics import semantic_env
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_postgresql import postgresql_engine


def submit(env, strategy):
    body = dict(question='return', target='answer_signal') if strategy == 'fast' else dict(question='cross-module call chain')
    response = env.r.context.client.post(f'/api/v1/projects/{env.r.own}/questions', headers=env.r.headers,
        json=body)
    assert response.status_code == 202, response.text
    return {key: UUID(value) for key,value in response.json().items() if key.endswith('_id')}


def service(env, uow):
    from backend.app.runtime import build_project_service
    projects = build_project_service(uow, adapter=env.app.state.adapter, workspace=env.app.state.workspace,
        artifacts=env.app.state.artifacts, operations=env.app.state.project_operations,
        embedding_settings_factory=env.settings_factory, embedding_provider_factory=env.factory,
        retrieval_settings_factory=lambda: RetrievalSettings('vector'))
    return build_agent_run_service(uow, background_tasks=BackgroundTaskService(uow), projects=projects,
        adapter=env.app.state.adapter, agent_workspace=env.app.state.agent_workspace)


def switch_current(env):
    root=env.app.state.workspace.path_for(env.r.own)
    with env.r.context.uow_factory() as uow:
        with uow.transaction(): newer=uow.snapshots.start(env.r.own,'b'*40)
        bundle=env.app.state.artifacts.build_and_publish(env.r.own,newer.id,newer.commit_sha,
            lambda i,g:env.app.state.adapter.index(root,index_path=i,graph_path=g,project_id=env.r.own,
                snapshot_id=newer.id,commit_sha=newer.commit_sha), require_chunks=True)
        with uow.transaction():
            uow.snapshots.mark_ready(newer.id,index_artifact_path=bundle.index_artifact_path,
                graph_artifact_path=bundle.graph_artifact_path,summary=bundle.summary)
            uow.projects.set_current_snapshot(env.r.own,newer.id)
    return newer


def expire(env, task):
    with env.r.context.uow_factory() as uow,uow.transaction():
        expired=datetime.now(timezone.utc)-timedelta(minutes=10)
        if hasattr(uow,'session'):
            uow.session.execute(update(BackgroundTaskORM).where(BackgroundTaskORM.id==task.id).values(lease_expires_at=expired))
        else:uow.state.background_tasks[task.id]=replace(task,lease_expires_at=expired)


@pytest.mark.parametrize('strategy',['fast','agent'])
def test_question_fixed_snapshot_provider_marker_transaction_and_redelivery(vector_env,monkeypatch,strategy):
    env=vector_env; ids=submit(env,strategy); newer=switch_current(env)
    factory=env.factory
    def guarded_factory(settings):
        with env.r.context.uow_factory() as uow,uow.transaction():
            assert uow.agent_runs.get(ids['run_id']).core_started_at is not None
        return factory(settings)
    env.factory=guarded_factory
    generate=env.app.state.adapter.llm_client.generate
    def guarded_generate(prompt):
        assert not env.active_transactions and not env.memory_transactions
        with env.r.context.uow_factory() as uow,uow.transaction():
            assert uow.agent_runs.get(ids['run_id']).core_started_at is not None
        return generate(prompt)
    monkeypatch.setattr(env.app.state.adapter.llm_client,'generate',guarded_generate)
    with env.r.context.uow_factory() as uow:
        worker=service(env,uow)
        run=worker.execute(ids['task_id'])
        count=len(env.created)
        assert worker.execute(ids['task_id'])==run
    assert run.status=='completed' and run.snapshot_id==env.snapshot_id and run.snapshot_id!=newer.id
    assert run.execution_metadata['strategy']==strategy
    assert env.created and len(env.created)==count and all(p.closed for p in env.created)
    assert run.result_metadata['binding']['snapshot_id']==str(env.snapshot_id)
    if strategy=='fast':
        assert count==1 and sum(len(p.queries) for p in env.created)==1
        assert run.result_metadata['references']
    assert snapshot(env).status=='ready'


@pytest.mark.parametrize('post_marker',[False,True])
def test_question_recovery_after_snapshot_switch_uses_saved_decision_and_no_post_marker_retry(vector_env,post_marker):
    env=vector_env; ids=submit(env,'fast')
    with env.r.context.uow_factory() as uow,uow.transaction():
        task=uow.background_tasks.claim(ids['task_id']).task
        run=uow.agent_runs.mark_running(ids['run_id'])
        if post_marker:uow.agent_runs.mark_core_started(run.id)
    switch_current(env);expire(env,task)
    with env.r.context.uow_factory() as uow:
        result=StaleRunningReconciler(uow,env.app.state.project_operations,StaleReconciliationSettings()).reconcile()
        with uow.transaction(): current=uow.agent_runs.get(run.id)
        assert current.snapshot_id==env.snapshot_id and current.execution_metadata==run.execution_metadata
        if post_marker:
            assert result.failed==1 and current.status=='failed' and not env.created
        else:
            assert result.requeued==1 and current.status=='queued' and not env.created
            assert service(env,uow).execute(ids['task_id']).status=='completed'
