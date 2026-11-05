from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID

from backend.app.core.errors import BackendError
from backend.app.core.run_metadata import question_metadata, result_for
from pycode.question import route_question
from test_b01_users import uow_factory
from test_b02_auth import auth_context
from test_b04_project_derived_auth import resources
from test_b11_admission import rate_redis, limited, metadata, counts
from test_postgresql import postgresql_engine


def test_question_uses_existing_agent_quota_once_and_replays_while_archived(limited):
    r=limited;metadata(r)
    url=f'/api/v1/projects/{r.own}/questions'
    headers={**r.headers,'Idempotency-Key':'question-quota'}
    first=r.context.client.post(url,headers=headers,json={'question':'explain main.py'})
    assert first.status_code==202,first.text
    before,calls=counts(r.context),len(r.calls)
    assert r.context.client.post(url,headers=headers,json={'question':'explain main.py'}).json()==first.json()
    assert counts(r.context)==before and len(r.calls)==calls
    assert r.redis.get(f'pycode:rate:v1:{r.owner}:agent')==b'1'
    denied=r.context.client.post(url,headers=r.headers,json={'question':'another task'})
    assert denied.status_code==429 and counts(r.context)==before
    r.context.client.post(f'/api/v1/projects/{r.own}/archive',headers=r.headers)
    assert r.context.client.post(url,headers=headers,json={'question':'explain main.py'}).json()==first.json()
    assert len(r.calls)==calls+1  # Only the rejected new request touched the rate limiter.


def test_parallel_question_replay_has_one_run_and_one_saved_decision(resources):
    r=resources;metadata(r);barrier=Barrier(3)
    def submit(_):
        barrier.wait(timeout=30)
        return r.context.client.post(f'/api/v1/projects/{r.own}/questions',
            headers={**r.headers,'Idempotency-Key':'same-question'},json={'question':'explain main.py'})
    with ThreadPoolExecutor(max_workers=3) as pool: replies=list(pool.map(submit,range(3)))
    assert all(reply.status_code==202 for reply in replies),[r.status_code for r in replies]
    assert len({reply.json()['run_id'] for reply in replies})==1
    with r.context.uow_factory() as uow,uow.transaction():
        run=uow.agent_runs.get(UUID(replies[0].json()['run_id']))
        assert run.execution_metadata['strategy']=='fast' and run.status=='queued'


def test_v2_terminal_text_and_metadata_remain_atomic_with_competing_writers(uow_factory):
    with uow_factory() as uow,uow.transaction():
        project=uow.projects.create('question','https://github.com/example/demo.git')
        snap=uow.snapshots.start(project.id,'a'*40)
        request,execution=question_metadata(snap,route_question('main.py'),None)
        run=uow.agent_runs.create_queued(project.id,snap.id,task='main.py',run_type='question',model=None,
            request_metadata=request,execution_metadata=execution)
        run=uow.agent_runs.mark_running(run.id)
    barrier=Barrier(2)
    def finish(fail):
        barrier.wait(timeout=30)
        try:
            with uow_factory() as uow,uow.transaction():
                return uow.agent_runs.fail(run.id,'safe failure',result_metadata=result_for(run,failed=True)) if fail else uow.agent_runs.complete(run.id,'answer',result_metadata=result_for(run))
        except BackendError as exc:
            assert exc.code=='agent_run_state_conflict'
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(finish,[False,True]))
    assert sum(result is not None for result in results)==1
    with uow_factory() as uow,uow.transaction():
        current=uow.agent_runs.get(run.id)
        assert current.result_metadata['outcome']==current.status
        assert current.execution_metadata==execution
        assert current.result==('answer' if current.status=='completed' else None)
