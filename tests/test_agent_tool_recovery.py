"""A05b contracts and finite recovery with deterministic, offline tools/models."""
import errno
import json
import subprocess

import pytest

from pycode.agent.budget import ExecutionBudget, ExecutionLimits, ExecutionStopped
from pycode.agent.executor import execute_tool_call
from pycode.agent.progress import ProgressTracker
from pycode.agent.runtime import run_agent_runtime
from pycode.agent.types import AgentTask, RuntimeConfig, ToolCall
from pycode.tools import TOOLS, ToolContext, ToolSpec
from pycode.tools.base import ToolExecution, ToolResult, failure, normalize_tool_result, success
from pycode.tools.test_runner import run_pytest


class Replies:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        assert self.responses, 'Unexpected model call after completion/stop'
        return self.responses.pop(0)


def plan(tool='idle', *, required=False):
    return json.dumps([dict(tool=tool, arguments={}, required=required)])


def action(tool, **arguments):
    return json.dumps(dict(action_type='tool_call', tool_name=tool, arguments=arguments))


FINAL = json.dumps(dict(action_type='final_answer', final_answer='Evidence-based answer'))


def runtime(root, model, tools):
    return run_agent_runtime(AgentTask('Inspect repository', root),
        RuntimeConfig(enable_memory=False), tools=tools, llm_client=model)


SCHEMA = dict(type='object', properties={
    'name': dict(type='string', minLength=2, maxLength=5),
    'count': dict(type='integer', minimum=1, maximum=3),
    'mode': dict(type='string', enum=['read','list']),
    'items': dict(type='array', minItems=1, maxItems=2, items=dict(type='object',
        properties={'enabled':dict(type='boolean')}, required=['enabled'], additionalProperties=False)),
    'target': dict(type=['string','null']),
}, required=['name','count','mode','items'], additionalProperties=False)


@pytest.mark.parametrize('change', [
    {'name':'x'}, {'name':'secret-too-long'}, {'count':True}, {'count':1.5}, {'count':0}, {'count':4},
    {'mode':'secret-invalid'}, {'items':[]}, {'items':[{}, {}, {}]}, {'items':[{'enabled':1}]},
    {'items':[{'enabled':True,'secret':1}]}, {'target':3}, {'extra':'secret'},
])
def test_schema_rejects_before_handler_without_input_reflection(tmp_path, change):
    def forbidden(*a, **k):
        pytest.fail('Invalid input reached handler')
    spec = ToolSpec('inspect', forbidden, input_schema=SCHEMA)
    args = dict(name='valid', count=1, mode='read', items=[dict(enabled=True)], target=None)
    args.update(change)
    result = execute_tool_call(AgentTask('Inspect',tmp_path), ToolCall('inspect',args), tools={'inspect':spec})
    assert not result.ok and result.execution.category == 'invalid_arguments'
    assert result.execution.attempts == 0 and not result.execution.retryable
    assert 'secret' not in str(result.error) and 'arguments' not in result.data


def test_schema_nullable_nested_values_and_legacy_empty_schema(tmp_path):
    calls = []
    def handler(context, **kw):
        calls.append(kw)
        return success('inspect','Observed')
    spec = ToolSpec('inspect',handler,input_schema=SCHEMA)
    args = dict(name='valid',count=3,mode='list',items=[dict(enabled=False)],target=None)
    result = execute_tool_call(AgentTask('Inspect',tmp_path),ToolCall('inspect',args),tools={'inspect':spec})
    assert result.ok and calls == [args]
    assert execute_tool_call(AgentTask('Inspect',tmp_path),ToolCall('inspect',{'legacy':'accepted'}),
        tools={'inspect':ToolSpec('inspect',handler)}).ok


@pytest.mark.parametrize('bad', [None, {}, ToolResult('wrong',True,'Observed'),
    ToolResult('inspect',True,'Observed',error='contradiction')])
def test_invalid_tool_outputs_are_failures_not_executor_exceptions(tmp_path, bad):
    result = execute_tool_call(AgentTask('Inspect',tmp_path),ToolCall('inspect',{}),
        tools={'inspect':ToolSpec('inspect',lambda context:bad)})
    assert not result.ok and result.execution.category == 'internal_error'


@pytest.mark.parametrize('error,category', [
    (PermissionError('secret'), 'permission_denied'), (FileNotFoundError('secret'),'not_found'),
    (TimeoutError('secret'),'timeout'), (RuntimeError('secret'),'internal_error'),
])
def test_errors_have_safe_categories_and_default_no_retry(tmp_path,error,category):
    calls=[]
    def handler(context):
        calls.append(1)
        raise error
    result=execute_tool_call(AgentTask('Inspect',tmp_path),ToolCall('inspect',{}),
        tools={'inspect':ToolSpec('inspect',handler)})
    assert not result.ok and calls == [1] and result.execution.category == category
    assert 'secret' not in result.safe_summary + result.safe_error
    assert result.execution.attempts == 1 and not result.execution.retryable
    if category == 'permission_denied':
        assert result.data['denied'] and result.data['denied_by'] == 'other'


def retry_spec(handler):
    return ToolSpec('inspect',handler,max_attempts=2,retry_on=('transient_io',))


def test_safe_retry_counts_each_attempt_in_same_budget(tmp_path):
    calls=[]
    def handler(context):
        calls.append(1)
        if len(calls)==1:raise OSError(errno.EAGAIN,'temporary')
        return success('inspect','Recovered')
    budget=ExecutionBudget()
    with budget.activate():
        result=execute_tool_call(AgentTask('Inspect',tmp_path),ToolCall('inspect',{}),
            tools={'inspect':retry_spec(handler)})
    assert result.ok and calls == [1,1] and result.execution.attempts == 2
    assert budget.usage['tool_calls'] == 2 and budget.usage['model_calls'] == 0
    assert not result.execution.retryable
    assert [name for name,spec in TOOLS.items() if spec.max_attempts>1] == ['read_file']


def test_safe_retry_is_finite_and_permission_never_retried(tmp_path):
    for error,expected in [(OSError(errno.EINTR,'temporary'),2),(PermissionError('denied'),1)]:
        calls=[]
        def handler(context):
            calls.append(1)
            raise error
        result=execute_tool_call(AgentTask('Inspect',tmp_path),ToolCall('inspect',{}),
            tools={'inspect':retry_spec(handler)})
        assert not result.ok and len(calls) == result.execution.attempts == expected


@pytest.mark.parametrize('flags',[dict(read_only=False),dict(writes_internal_state=True),
    dict(destructive=True),dict(requires_confirmation=True)])
def test_stateful_and_unconfirmed_tools_cannot_enable_retry(flags):
    with pytest.raises(ValueError,match='retry policy'):
        ToolSpec('write',lambda c:None,max_attempts=2,retry_on=('timeout',),**flags)


@pytest.mark.parametrize('stop',['budget_tools','budget_time','execution_lost'])
def test_retry_respects_remaining_budget_deadline_and_ownership(tmp_path,stop):
    calls=[]; now=[0.0]; owned=[True]
    def checkpoint():
        if not owned[0]:raise RuntimeError('Lease lost')
    budget=ExecutionBudget(ExecutionLimits(max_tool_calls=1 if stop=='budget_tools' else 8,
        max_runtime_seconds=1),clock=lambda:now[0],checkpoint=checkpoint)
    def handler(context):
        calls.append(1)
        if stop=='budget_time':now[0]=2
        if stop=='execution_lost':owned[0]=False
        raise OSError(errno.EAGAIN,'temporary')
    with budget.activate(),pytest.raises(ExecutionStopped) as caught:
        execute_tool_call(AgentTask('Inspect',tmp_path),ToolCall('inspect',{}),tools={'inspect':retry_spec(handler)})
    assert caught.value.reason == stop and calls == [1] and budget.usage['tool_calls'] == 1


@pytest.mark.parametrize('code',[0,1,2,5])
def test_test_process_exit_status_is_authoritative(tmp_path,monkeypatch,code):
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:subprocess.CompletedProcess(a[0],code,'test output',''))
    result=run_pytest(ToolContext(tmp_path,allow_tests=True))
    assert result.ok is (code==0) and result.data['exit_code']==code
    assert result.execution.category == ('success' if code==0 else 'test_failed')


def test_test_timeout_bounds_bytes_output_and_retains_explicit_truncation(tmp_path,monkeypatch):
    def timeout(*a,**k):raise subprocess.TimeoutExpired(a[0],1,output=b'x'*100,stderr=b'y'*100)
    monkeypatch.setattr(subprocess,'run',timeout)
    result=execute_tool_call(AgentTask('Test',tmp_path,allow_tests=True),ToolCall('run_tests',{}),
        context=ToolContext(tmp_path,allow_tests=True,max_output_chars=12))
    assert not result.ok and result.execution.category=='timeout' and result.execution.truncated
    assert len(result.data['stdout'])==len(result.data['stderr'])==12
    assert result.data['stdout_truncated'] and result.data['stderr_truncated']
    assert result.execution.attempts == 1


def test_required_failure_cannot_be_overridden_by_final_answer_or_todo_write(tmp_path):
    for forge_todo in (False,True):
        responses=[plan('read_file',required=True),action('read_file')]
        if forge_todo:responses.append(action('todo_write',operation='set_status',todo_id='todo-1',status='completed'))
        responses.append(FINAL)
        model=Replies(*responses)
        tools={'read_file':ToolSpec('read_file',lambda c:failure('read_file','secret','secret')),
               'todo_write':TOOLS['todo_write']}
        result=runtime(tmp_path,model,tools)
        assert not result.ok and result.stop_reason=='required_tool_failed' and result.answer is None
        assert result.required_failures==1 and result.execution_report['required_failed']==1
        assert len(model.prompts)==len(responses)
        assert all('secret' not in p for p in model.prompts)
        assert 'secret' not in result.prompt and 'secret' not in json.dumps(result.trace.to_dict())


def test_actual_successful_retry_can_resolve_required_failure(tmp_path):
    results=iter([failure('read_file','Failed','private'),success('read_file','Recovered',content='new code')])
    model=Replies(plan('read_file',required=True),action('read_file'),action('read_file'),FINAL)
    result=runtime(tmp_path,model,{'read_file':ToolSpec('read_file',lambda c:next(results))})
    assert result.ok and result.required_failures==0 and result.answer
    assert result.todos[0].status=='completed' and len(result.tool_results)==2


def test_empty_required_observation_is_explicit_failure(tmp_path):
    result=runtime(tmp_path,Replies(plan('read_file',required=True),action('read_file'),FINAL),
        {'read_file':ToolSpec('read_file',lambda c:success('read_file','Empty',content=''))})
    assert not result.ok and result.stop_reason=='required_tool_failed'
    assert result.tool_results[0].execution.category=='empty' and result.todos[0].status=='failed'


def test_success_for_different_target_cannot_erase_required_failed_operation(tmp_path):
    def read(context,file_path):
        return failure('read_file','Failed','private') if file_path=='required.py' else success('read_file','Other',content='other code')
    model=Replies(plan('read_file',required=True),action('read_file',file_path='required.py'),
                  action('read_file',file_path='other.py'),FINAL)
    result=runtime(tmp_path,model,{'read_file':ToolSpec('read_file',read)})
    assert not result.ok and result.required_failures==1 and result.answer is None


def idle(context):return success('idle','Unused')


def test_unbound_repeated_failure_stops_without_summary_or_memory(tmp_path):
    model=Replies(plan(),action('search_code',query='q'),action('search_code',query='q'))
    tools={'idle':ToolSpec('idle',idle),'search_code':ToolSpec('search_code',lambda c,**k:failure('search_code','Failed','private'))}
    result=runtime(tmp_path,model,tools)
    assert not result.ok and result.stop_reason=='no_progress' and len(result.tool_results)==2
    assert len(model.prompts)==3 and result.answer is None and result.memory is None
    assert result.execution_report['outcome']=='failed'


def test_changed_queries_with_new_evidence_are_not_stopped(tmp_path):
    model=Replies(plan(),*[action('search_code',query=q) for q in ('a','b','c')],FINAL)
    tools={'idle':ToolSpec('idle',idle),'search_code':ToolSpec('search_code',
        lambda c,query:success('search_code','Found',matches=[dict(path=query+'.py',line_number=1)]))}
    result=runtime(tmp_path,model,tools)
    assert result.ok and len(result.tool_results)==3 and result.answer


def test_alternating_calls_cannot_manufacture_progress_from_same_evidence(tmp_path):
    model=Replies(plan(),*[action('search_code',query=q) for q in ('a','b','a','b')])
    tools={'idle':ToolSpec('idle',idle),'search_code':ToolSpec('search_code',
        lambda c,query:success('search_code','Found',matches=[dict(path='same.py',line_number=1)]))}
    result=runtime(tmp_path,model,tools)
    assert not result.ok and result.stop_reason=='no_progress' and len(result.tool_results)==3
    assert result.execution_report['outcome']=='partial'


def test_stateful_repeat_blocked_before_second_side_effect_even_after_failure(tmp_path):
    calls=[]
    def stateful(context):
        calls.append(1)
        return failure('write','Failed','private')
    model=Replies(plan('write'),action('write'),action('write'))
    result=runtime(tmp_path,model,{'write':ToolSpec('write',stateful,read_only=False,writes_internal_state=True)})
    assert calls==[1] and not result.ok and result.stop_reason=='no_progress'


def test_canonical_identity_normalizes_defaults_but_keeps_trusted_binding():
    spec=ToolSpec('read_file',lambda c,file_path,start_line=1:None)
    a=ToolCall('read_file',{'file_path':'a.py'})
    b=ToolCall('read_file',{'start_line':1,'file_path':'a.py'})
    first=ProgressTracker(dict(project_id='one',snapshot_id='one',commit_sha='a'))
    second=ProgressTracker(dict(project_id='one',snapshot_id='two',commit_sha='a'))
    assert first.key(a,spec)==first.key(b,spec) and first.key(a,spec)!=second.key(a,spec)


def test_optional_failure_and_ordinary_no_tool_answer_remain_compatible(tmp_path):
    tools={'idle':ToolSpec('idle',lambda c:failure('idle','Failed','private'))}
    for responses in ((plan(),FINAL),(plan(),action('idle'),FINAL)):
        result=runtime(tmp_path,Replies(*responses),tools)
        assert result.ok and result.answer and result.required_failures==0


def test_tool_execution_rejects_unknown_or_unbounded_contract():
    for values in (dict(category='secret'),dict(category='failed',attempts=3),
                   dict(category='success',schema_version=2),dict(category='failed',attempts=True)):
        with pytest.raises(ValueError):ToolExecution(**values)


def test_todo_mutations_alone_cannot_manufacture_runtime_progress(tmp_path):
    steps=json.dumps([dict(tool='todo_write',arguments={},required=False)]*3)
    model=Replies(steps,*[action('todo_write',operation='set_status',todo_id='todo-3',status=status)
                          for status in ('pending','in_progress','pending')])
    result=runtime(tmp_path,model,{'todo_write':TOOLS['todo_write']})
    assert result.stop_reason=='no_progress' and not result.ok and len(result.tool_results)==3


def test_turn_budget_keeps_precedence_over_required_tool_failure(tmp_path):
    model=Replies(plan('read_file',required=True),action('read_file'))
    result=run_agent_runtime(AgentTask('Inspect',tmp_path),RuntimeConfig(max_turns=1,enable_memory=False),
        llm_client=model,tools={'read_file':ToolSpec('read_file',lambda c:failure('read_file','Failed','private'))})
    assert result.stop_reason=='max_turns' and result.execution_budget['stop_reason']=='max_turns'
    assert not result.ok and result.required_failures==1


def test_sensitive_tool_arguments_are_not_copied_into_trace_or_error_prompts(tmp_path):
    calls=[]
    def inspect(context,**arguments):
        calls.append(arguments)
        raise RuntimeError('PRIVATE_CREDENTIAL')
    model=Replies(plan('inspect'),action('inspect',api_key='PRIVATE_CREDENTIAL'),FINAL)
    result=runtime(tmp_path,model,{'inspect':ToolSpec('inspect',inspect)})
    assert calls==[{'api_key':'PRIVATE_CREDENTIAL'}]  # handler input is not rewritten
    assert 'PRIVATE_CREDENTIAL' not in json.dumps(result.trace.to_dict())
    assert all('PRIVATE_CREDENTIAL' not in p for p in model.prompts)
    assert result.trace.tools[0].arguments['api_key']=='[REDACTED]'
