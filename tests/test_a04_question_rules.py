from uuid import uuid4

import pytest

from backend.app.core.run_metadata import checked, question_metadata, validated
from backend.app.core.errors import BackendError
from backend.app.schemas.projects import QuestionRequest
from pycode import question


@pytest.mark.parametrize('text,target,strategy,intent', [
    ('explain src/main.py', None, 'fast', 'explain'),
    ('impact of changing src/main.py', None, 'fast', 'impact'),
    ('修改 main.py 的影响', None, 'fast', 'impact'),
    ('dependencies of main.py', None, 'fast', 'dependency'),
    ('where is the entrypoint?', None, 'fast', 'entry'),
    ('overview', None, 'fast', 'onboard'),
    ('Explain symbol', 'Service.run', 'fast', 'general'),
    ('Explain `Service.run`', None, 'fast', 'general'),
    ('cross-module call chain in main.py', None, 'agent', 'general'),
    ('逐步排查 main.py', None, 'agent', 'general'),
    ('uncertain task', None, 'agent', 'general'),
    ('mainstream application', None, 'agent', 'general'),
])
def test_bounded_rules_are_repeatable(text, target, strategy, intent):
    first = question.route_question(text, target)
    assert first == question.route_question(text, target)
    assert (first.strategy, first.intent) == (strategy, intent)
    assert first.reasons and first.policy_version == 'a04b-v1'


def test_rule_failure_is_safe_but_invalid_requests_are_not_swallowed(monkeypatch):
    def fail(*args): raise RuntimeError('private implementation exception')
    monkeypatch.setattr(question, '_route', fail)
    decision = question.route_question('main.py')
    assert decision.strategy == 'agent' and decision.reasons == ('routing_fallback',)
    with pytest.raises(ValueError): question.route_question('')
    with pytest.raises(ValueError): question.route_question('x', '../private')


@pytest.mark.parametrize('body', [dict(question='x', strategy='fast'), dict(question='x', commit_sha='f'*40),
    dict(question='x', target='../file'), dict(question='x'*8001), dict(question='x', target='a'*201),
    dict(question='x', model='a'*129), dict(question='x', snapshot_id='fake')])
def test_question_input_rejects_untrusted_fields(body):
    with pytest.raises(ValueError): QuestionRequest(**body)


def test_v2_decision_bound_and_immutable_on_reads():
    from types import SimpleNamespace
    snapshot = SimpleNamespace(project_id=uuid4(), id=uuid4(), commit_sha='f'*40)
    request, execution = question_metadata(snapshot, question.route_question('impact main.py'), None)
    values = checked(snapshot.project_id, snapshot.id, 'question', 'queued', snapshot, request, execution, None)
    assert values['execution_metadata'] == execution
    assert execution['strategy'] == 'fast' and execution['target'] == 'main.py'
    for field, bad in [('request_metadata', dict(request,schema_version=99)),
                       ('execution_metadata', dict(execution,prompt='private'))]:
        with pytest.raises(BackendError): validated(field,bad)
    with pytest.raises(BackendError):
        checked(snapshot.project_id,snapshot.id,'question','queued',snapshot,request,None,None)
    with pytest.raises(BackendError):
        checked(snapshot.project_id,uuid4(),'question','queued',snapshot,request,execution,None)
