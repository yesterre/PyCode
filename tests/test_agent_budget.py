import json

import pytest

from pycode.agent.budget import (BudgetedLLM, ExecutionBudget, ExecutionLimits,
                                 ExecutionStopped, current_budget)
from pycode.agent.runtime import run_agent_runtime
from pycode.agent.types import AgentTask, RuntimeConfig
from pycode.tools import ToolSpec
from pycode.tools.base import success


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


class Model:
    def __init__(self):
        self.calls = []

    def generate(self, prompt):
        self.calls.append(prompt)
        if prompt.startswith('You are the PyCode LLM Planner.'):
            return json.dumps([dict(tool='retrieve_context', arguments={}, required=True)])
        if prompt.startswith('You are the PyCode LLM Next-Action Planner.'):
            return json.dumps(dict(action_type='tool_call', tool_name='retrieve_context', arguments={}))
        return 'Synthetic answer'


def run(tmp_path, limits, *, tool=None, model=None, planner=True, clock=None, checkpoint=None):
    budget = ExecutionBudget(limits, clock=clock or Clock(), checkpoint=checkpoint)
    handler = tool or (lambda context, **kwargs: success('retrieve_context', 'Synthetic evidence', evidence=['fixture.py']))
    with budget.activate():
        result = run_agent_runtime(AgentTask('entry point', tmp_path),
            RuntimeConfig(use_llm_planner=planner, enable_memory=False),
            tools={'retrieve_context': ToolSpec('retrieve_context', handler)}, llm_client=model)
    assert current_budget() is None
    return result


@pytest.mark.parametrize('change', [dict(max_turns=0), dict(max_model_calls=-1),
    dict(max_tool_calls=True), dict(max_runtime_seconds=float('nan')),
    dict(context_tokens=0), dict(tool_output_chars=1)])
def test_limits_reject_invalid_values_without_exposing_them(change):
    with pytest.raises(ValueError, match='Invalid Agent execution limits'):
        ExecutionLimits(**change)


def test_server_environment_is_bounded_and_does_not_reflect_invalid_values(monkeypatch):
    monkeypatch.setenv('PYCODE_AGENT_MAX_MODEL_CALLS', '2')
    assert ExecutionLimits.from_environment().max_model_calls == 2
    monkeypatch.setenv('PYCODE_AGENT_MAX_MODEL_CALLS', 'PRIVATE-invalid')
    with pytest.raises(ValueError) as caught:
        ExecutionLimits.from_environment()
    assert 'PRIVATE' not in str(caught.value)


@pytest.mark.parametrize('allowed', [0, 1])
def test_initial_plan_and_next_action_share_model_budget_without_fallback(tmp_path, allowed):
    model = Model()
    result = run(tmp_path, ExecutionLimits(max_model_calls=allowed), model=model)
    assert result.stop_reason == 'budget_models' and not result.ok
    assert len(model.calls) == allowed
    assert result.tool_results == []
    assert result.execution_budget['usage']['model_calls'] == allowed
    assert not any(e.event_type == 'LLMPlanFallback' for e in result.trace.events)


def test_summary_cannot_bypass_zero_model_budget_and_observations_are_preserved(tmp_path):
    model = Model()
    result = run(tmp_path, ExecutionLimits(max_model_calls=0), model=model, planner=False)
    assert result.stop_reason == 'budget_models' and not result.ok
    assert len(result.tool_results) == 1 and result.tool_results[0].ok
    assert model.calls == [] and result.answer is None
    assert result.execution_budget['usage']['tool_calls'] == 1


def test_zero_tool_budget_never_enters_handler(tmp_path):
    calls = []
    result = run(tmp_path, ExecutionLimits(max_tool_calls=0), planner=False,
        tool=lambda *a, **k: calls.append(1))
    assert result.stop_reason == 'budget_tools' and not result.ok
    assert not calls and result.execution_budget['usage']['tool_calls'] == 0


def test_deadline_after_tool_preserves_result_and_does_not_generate(tmp_path):
    clock, model = Clock(), Model()
    def slow(context, **kwargs):
        clock.now = 2
        return success('retrieve_context', 'Finished synthetic tool', evidence=['fixture.py'])
    result = run(tmp_path, ExecutionLimits(max_runtime_seconds=1), planner=False,
                 model=model, tool=slow, clock=clock)
    assert result.stop_reason == 'budget_time' and not result.ok
    assert len(result.tool_results) == 1 and result.tool_results[0].ok
    assert result.execution_budget['elapsed_ms'] == 2000 and not model.calls


def test_lost_ownership_is_not_a_provider_fallback_or_summary(tmp_path):
    model = Model()
    def lost():
        raise RuntimeError('PRIVATE ownership detail')
    result = run(tmp_path, ExecutionLimits(), model=model, checkpoint=lost)
    assert result.stop_reason == 'execution_lost' and not result.ok
    assert not model.calls and 'PRIVATE' not in json.dumps(result.execution_budget)


def test_memory_phases_use_the_same_model_proxy(tmp_path, monkeypatch):
    import pycode.agent.runtime as runtime
    calls = []
    def prepare(*args, llm_client, **kwargs):
        calls.append('memory_selection')
        llm_client.generate('memory_selection')
        return None, ''
    monkeypatch.setattr(runtime, '_prepare_memory_context', prepare)
    model = Model()
    result = run(tmp_path, ExecutionLimits(max_model_calls=1), model=model)
    assert calls == ['memory_selection'] and model.calls == ['memory_selection']
    assert result.stop_reason == 'budget_models'


def test_native_bounded_generation_receives_remaining_time_and_stops_after_overrun():
    clock = Clock()
    budget = ExecutionBudget(ExecutionLimits(max_runtime_seconds=2), clock=clock)
    timeouts = []
    class Native:
        def generate_bounded(self, prompt, *, timeout_seconds):
            timeouts.append(timeout_seconds)
            clock.now = 3
            return 'Not accepted after deadline'
    with pytest.raises(ExecutionStopped) as caught:
        BudgetedLLM(Native(), budget).generate('synthetic')
    assert caught.value.reason == 'budget_time' and timeouts == [2]
    assert budget.usage['model_calls'] == 1


def test_budget_scope_cannot_be_replaced_or_cross_snapshot_and_resets():
    first = ExecutionBudget(binding=('project-a','snapshot-a','commit'))
    second = ExecutionBudget(binding=('project-a','snapshot-b','commit'))
    with first.activate():
        with pytest.raises(ValueError), second.activate():
            pass
        with pytest.raises(ValueError):
            first.validate_binding(second.binding)
        with first.activate():
            first.consume('query_embeddings')
        assert current_budget() is first and first.usage['query_embeddings'] == 1
    assert current_budget() is None


def test_turn_limit_blocks_another_decision_and_no_extra_summary(tmp_path):
    model = Model()
    result = run(tmp_path, ExecutionLimits(max_turns=1), model=model)
    assert result.stop_reason == 'max_turns' and not result.ok
    assert len(model.calls) == 2 and len(result.tool_results) == 1
    assert result.execution_budget['usage']['turns'] == 1


def test_context_and_output_limits_are_explicitly_reserved_not_silent_truncation(tmp_path):
    payload = 'X' * 1000
    result = run(tmp_path, ExecutionLimits(tool_output_chars=256), planner=False,
        tool=lambda *a, **k: success('retrieve_context','Synthetic', content=payload))
    assert result.tool_results[0].data['content'] == payload
    assert result.execution_budget['context_output_enforcement'] == 'enforced_v1'
    assert payload not in result.prompt
    assert result.context_report['reason_counts']['tool_output_limit'] == 1
    assert result.context_report['estimated_tokens'] <= result.context_report['budget']


def test_public_trace_budget_refuses_private_extra_fields_and_impossible_usage():
    from backend.app.core.application_trace import public_harness_budget
    value = ExecutionBudget().snapshot()
    assert public_harness_budget(value) == value
    assert public_harness_budget(dict(value, prompt='PRIVATE')) is None
    assert public_harness_budget(dict(value, usage=dict(value['usage'], model_calls=999))) is None
    assert public_harness_budget(dict(value, stop_reason={'secret':'PRIVATE'})) is None


def test_memory_extraction_cannot_write_after_model_budget_is_spent(tmp_path, monkeypatch):
    from pycode.agent import memory
    from pycode.agent.memory import MemoryStore
    writes, calls = [], []
    monkeypatch.setattr(MemoryStore, 'add_memory', lambda *a, **k: writes.append(k))
    class Summary:
        def generate(self, prompt):
            calls.append(prompt)
            return 'Synthetic answer'
    budget = ExecutionBudget(ExecutionLimits(max_model_calls=1), clock=Clock())
    with budget.activate():
        result = run_agent_runtime(AgentTask('entry point', tmp_path),
            RuntimeConfig(use_llm_planner=False), llm_client=Summary(),
            tools={'retrieve_context':ToolSpec('retrieve_context',lambda *a, **k:
                success('retrieve_context','Synthetic current code',evidence=['fixture.py']))})
    assert result.stop_reason == 'budget_models' and len(calls) == 1
    assert not writes and len(result.tool_results) == 1


def test_native_llm_timeout_caps_each_phase_preserves_retry_limit_and_closes(monkeypatch):
    from types import SimpleNamespace
    from backend.app.integrations.llm import BackendOpenAIClient
    from backend.app.config import LLMTransportSettings
    import backend.app.integrations.llm as module
    seen, closed = [], []
    monkeypatch.setattr(module,'load_llm_settings',lambda *a:dict(OPENAI_API_KEY='test-only',OPENAI_MODEL='test-only'))
    class Native:
        def __init__(self, **kwargs):
            seen.append(kwargs)
            self.responses = SimpleNamespace(create=lambda **k: SimpleNamespace(output_text='Synthetic'))
        def close(self):
            closed.append(True)
    monkeypatch.setattr('openai.OpenAI',Native)
    client = BackendOpenAIClient(LLMTransportSettings())
    assert client.generate_bounded('Synthetic',timeout_seconds=1.5) == 'Synthetic'
    timeout = seen[0]['timeout']
    assert all(getattr(timeout,k) <= 1.5 for k in ['connect','read','write','pool'])
    assert seen[0]['max_retries'] == 1 and closed == [True]


def test_query_provider_native_timeout_and_counter_do_not_modify_published_identity():
    from types import SimpleNamespace
    from backend.app.config import EmbeddingSettings
    from backend.app.integrations.embedding import TencentEmbeddingProvider, query_embedding_settings, embed_query
    calls = []
    class SDK:
        def __init__(self):
            self.embeddings = SimpleNamespace(create=self.create)
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(model='kinfra-text-embedding-0.6b',
                data=[SimpleNamespace(index=0,embedding=[1.0]*1024)],usage=None)
        def close(self):
            pass
    clock = Clock()
    budget = ExecutionBudget(ExecutionLimits(max_runtime_seconds=2),clock=clock)
    settings = EmbeddingSettings(enabled=True,api_key='test-only')
    with budget.activate():
        limited = query_embedding_settings(settings)
        provider = TencentEmbeddingProvider(limited,client=SDK())
        try:
            result = embed_query(provider,'Synthetic')
        finally:
            provider.close()
    assert result.model == settings.model and result.dimension == 1024
    assert limited.timeout_seconds == calls[0]['timeout'] == 2
    assert settings.timeout_seconds == 60 and budget.usage['query_embeddings'] == 1


def test_subprocess_native_timeout_cannot_exceed_remaining_run_budget(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from pycode.tools.test_runner import run_pytest
    from pycode.tools.base import ToolContext
    seen = []
    def process(*args, **kwargs):
        seen.append(kwargs['timeout'])
        return SimpleNamespace(returncode=0,stdout='',stderr='')
    monkeypatch.setattr('pycode.tools.test_runner.subprocess.run',process)
    budget = ExecutionBudget(ExecutionLimits(max_runtime_seconds=2),clock=Clock())
    with budget.activate():
        result = run_pytest(ToolContext(tmp_path,allow_tests=True),timeout=60)
    assert result.ok and seen == [2]


def test_fast_answer_reuses_an_already_bound_client_without_double_counting():
    from pycode.engine import _answer
    from pycode.retrieval_evidence import RetrievalResult
    model = Model()
    budget = ExecutionBudget(ExecutionLimits(max_model_calls=1),clock=Clock())
    with budget.activate():
        result = _answer(RetrievalResult('Synthetic question','general'),None,BudgetedLLM(model,budget))
    assert result.answer == 'Synthetic answer' and len(model.calls) == 1
    assert budget.usage['model_calls'] == 1
