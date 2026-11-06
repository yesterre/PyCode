"""Private deterministic progress tracking for the existing synchronous loop."""
from dataclasses import asdict, is_dataclass
import hashlib
import inspect
import json


CODE_TOOLS = frozenset({'retrieve_context','read_file','search_code','query_graph',
                        'git_diff','changed_files','run_tests'})


def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False,
        default=str,allow_nan=False).encode('utf-8')).hexdigest()


def _stable(value):
    if isinstance(value,dict):
        return {k:_stable(v) for k,v in value.items()
                if k not in {'duration_ms','initial_evidence_reused','initial_pack_digest',
                             'score','scores','reason','reasons','rank','fusion_score','source_scores'}}
    if isinstance(value,(list,tuple)):
        return [_stable(v) for v in value]
    return value


class ProgressBlocked(RuntimeError):
    pass


class ProgressTracker:
    def __init__(self, binding=None):
        self.binding = asdict(binding) if is_dataclass(binding) else binding
        self.epoch = 0
        self._evidence = set()
        self._completed = set()
        self._attempted_todos = set()
        self._calls = {}
        self._stagnant = 0
        self.repeated = False

    def key(self, call, spec):
        defaults = {}
        if spec is not None:
            try:
                for name,param in list(inspect.signature(spec.handler).parameters.items())[1:]:
                    if param.default is not inspect.Parameter.empty:
                        defaults[name] = param.default
            except (TypeError,ValueError):
                pass
        defaults.update(call.arguments)
        # Deprecated parameter is deliberately ignored by the retrieval tool.
        if call.name == 'retrieve_context':
            defaults.pop('max_snippet_chars',None)
            for name in ('intent','target'):
                if defaults.get(name) in ('',None):defaults[name]=None
        return _hash([self.binding,call.name,defaults])

    def before(self, call, spec, todo_id):
        if spec is None or (spec.read_only and not spec.writes_internal_state):
            return
        key = self.key(call,spec)
        prior = self._calls.get(key)
        if prior and prior[1] == self.epoch and (todo_id is None or todo_id in self._attempted_todos):
            self.repeated = True
            raise ProgressBlocked('Repeated stateful action has no new progress.')

    def observe(self, call, result, spec):
        key = self.key(call,spec)
        category = result.execution.category if result.execution else ('success' if result.ok else 'failed')
        todo_id = call.planned_todo_id
        if todo_id is not None:
            self._attempted_todos.add(todo_id)
        planned_progress = (result.ok and category != 'empty' and call.name != 'todo_write'
                            and todo_id is not None and todo_id not in self._completed)
        if planned_progress:self._completed.add(todo_id)
        evidence_progress = False
        if result.ok and category != 'empty' and call.name in CODE_TOOLS:
            data = result.data
            # Hash the observation, not query text, timing, score or display reasons.
            selected = {name:data[name] for name in ('items','matches','files','evidence','nodes','edges','content','diff','path',
                        'start_line','end_line','stdout','stderr','exit_code') if name in data}
            if selected:
                fingerprint = _hash([call.name,_stable(selected)])
                evidence_progress = fingerprint not in self._evidence
                self._evidence.add(fingerprint)
        prior = self._calls.get(key)
        if planned_progress or evidence_progress:
            self.epoch += 1
            self._stagnant = 0
            repeats = 1
        else:
            self._stagnant += 1
            repeats = prior[0]+1 if prior and prior[1] == self.epoch else 1
        self._calls[key] = (repeats,self.epoch)
        self.repeated = repeats >= 2
        return self.repeated or self._stagnant >= 3


def execution_report(result):
    outcomes = [r.execution for r in result.tool_results if r.execution is not None]
    failures = sum(not r.ok for r in result.tool_results)
    required = result.required_failures
    completed = sum(r.ok and (r.execution is None or r.execution.category != 'empty') for r in result.tool_results)
    outcome = ('partial' if completed else 'failed') if not result.ok else ('partial' if failures else 'complete')
    return dict(schema_version=1,outcome=outcome,tool_calls=len(result.tool_results),
        handler_attempts=sum(o.attempts for o in outcomes),retried_tools=sum(o.attempts>1 for o in outcomes),
        completed_tools=completed,failed_tools=failures,empty_tools=sum(o.category=='empty' for o in outcomes),
        required_failed=required,stop_reason=str(result.stop_reason))
