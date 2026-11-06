"""One trusted, call-scoped budget; no retrieval, persistence or retry policy."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import math
import os
from time import monotonic


STOP_REASONS = frozenset({'max_turns', 'budget_turns', 'budget_tools', 'budget_models',
                          'budget_embeddings', 'budget_time', 'execution_lost'})


class ExecutionStopped(BaseException):
    """Internal control transfer, deliberately outside provider/fallback Exception handlers.

    Only the Runtime and backend execution scope consume this signal. It must
    never become a retry, keyword degradation, tool error, or Memory fallback.
    """

    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class ExecutionLimits:
    max_turns: int = 8
    max_tool_calls: int = 8
    max_model_calls: int = 16
    max_query_embeddings: int = 8
    max_runtime_seconds: float = 300.0
    context_tokens: int = 131072
    tool_output_chars: int = 32768

    def __post_init__(self):
        for name, low, high in (('max_turns', 1, 128), ('max_tool_calls', 0, 256),
                               ('max_model_calls', 0, 256), ('max_query_embeddings', 0, 256),
                               ('context_tokens', 4096, 1048576), ('tool_output_chars', 256, 1048576)):
            value = getattr(self, name)
            if type(value) is not int or not low <= value <= high:
                raise ValueError('Invalid Agent execution limits')
        value = self.max_runtime_seconds
        if type(value) not in (int, float) or not math.isfinite(value) or not 0.01 <= value <= 3600:
            raise ValueError('Invalid Agent execution limits')

    @classmethod
    def from_environment(cls):
        """Server-only overrides; defaults and validation have a single source."""
        values = {}
        for name in cls.__dataclass_fields__:
            raw = os.environ.get('PYCODE_AGENT_' + name.upper())
            if raw is not None:
                try:
                    values[name] = float(raw) if name == 'max_runtime_seconds' else int(raw)
                except (ValueError, TypeError):
                    raise ValueError('Invalid Agent execution limits') from None
        return cls(**values)


_ACTIVE = ContextVar('pycode_execution_budget', default=None)


def current_budget():
    return _ACTIVE.get()


class ExecutionBudget:
    def __init__(self, limits=None, *, binding=None, checkpoint=None, clock=monotonic):
        self.limits = limits or ExecutionLimits()
        self.binding = binding
        self._checkpoint, self._clock = checkpoint, clock
        self._started = clock()
        self.usage = dict(turns=0, tool_calls=0, model_calls=0, query_embeddings=0)
        self.stop_reason = None
        self.ownership_error = None

    def stop(self, reason):
        if reason not in STOP_REASONS:
            raise ValueError('Unknown execution stop reason')
        self.stop_reason = self.stop_reason or reason
        raise ExecutionStopped(self.stop_reason)

    def check(self):
        if self.stop_reason:
            raise ExecutionStopped(self.stop_reason)
        if self._checkpoint is not None:
            try:
                self._checkpoint()
            except Exception as exc:
                self.ownership_error = exc
                self.stop('execution_lost')
        if self._clock() - self._started >= self.limits.max_runtime_seconds:
            self.stop('budget_time')

    def consume(self, kind):
        self.check()
        limit, reason = {'turns': ('max_turns', 'budget_turns'),
                         'tool_calls': ('max_tool_calls', 'budget_tools'),
                         'model_calls': ('max_model_calls', 'budget_models'),
                         'query_embeddings': ('max_query_embeddings', 'budget_embeddings')}[kind]
        if self.usage[kind] >= getattr(self.limits, limit):
            self.stop(reason)
        self.usage[kind] += 1

    def remaining_seconds(self):
        self.check()
        return max(0.001, self.limits.max_runtime_seconds - (self._clock() - self._started))

    def validate_binding(self, binding):
        if self.binding != binding:
            raise ValueError('Execution budget belongs to another Snapshot')

    @contextmanager
    def activate(self):
        existing = current_budget()
        if existing is not None and existing is not self:
            raise ValueError('Cannot replace an active execution budget')
        token = _ACTIVE.set(self)
        try:
            yield self
        finally:
            _ACTIVE.reset(token)

    def snapshot(self):
        return dict(schema_version=1, limits=asdict(self.limits), usage=dict(self.usage),
                    elapsed_ms=round(max(0, self._clock() - self._started) * 1000, 3),
                    stop_reason=self.stop_reason, context_output_enforcement='reserved_for_a05c',
                    transport_attempts='not_observed')


class BudgetedLLM:
    def __init__(self, client, budget):
        self.client, self.budget = client, budget

    def generate(self, prompt):
        self.budget.consume('model_calls')
        bounded = getattr(self.client, 'generate_bounded', None)
        result = (bounded(prompt, timeout_seconds=self.budget.remaining_seconds())
                  if callable(bounded) else self.client.generate(prompt))
        self.budget.check()
        return result


def budget_event(budget):
    from pycode.agent.trace import TraceEvent
    from pycode.agent._time_utils import utc_now
    return TraceEvent('ExecutionBudget', utc_now(), data={
        'included_section_names': [], 'skipped_section_names': [],
        'harness_budget': budget.snapshot()})
