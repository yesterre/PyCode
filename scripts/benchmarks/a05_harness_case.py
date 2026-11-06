"""Isolated A05 benchmark adapter. Calls the selected REAL Runtime, never a copy.

Only trusted Git-exported PyCode implementation code is imported. The fixed
repository corpus is read/parsed, never executed. No network/model credentials.
"""
from __future__ import annotations

from collections import Counter
from contextlib import nullcontext
from dataclasses import asdict, replace
import errno
import hashlib
import json
from pathlib import Path
import socket
import sys
from time import perf_counter
from uuid import NAMESPACE_URL, uuid5


def sha(value):
    return hashlib.sha256(value).hexdigest()


def contract_errors(row, expected):
    errors = []
    for key, value in expected.items():
        if key == 'fallback_min':
            valid = row['fallback_events'] >= value
        elif key == 'discard_reason':
            valid = (row['discard_reasons'] or {}).get(value, 0) > 0
        else:
            valid = row.get(key) == value
        if not valid:
            errors.append(key)
    return errors


def block_network():
    def forbidden(*args, **kwargs):
        raise AssertionError('Network is forbidden in the offline A05 benchmark')
    socket.socket.connect = forbidden
    socket.create_connection = forbidden


def run_case(dataset, case, corpus, work, implementation):
    # Imports are intentionally local: the launcher sets sys.path before here.
    from pycode.agent.runtime import run_agent_runtime
    from pycode.agent.types import AgentTask, RuntimeConfig
    from pycode.chunks import SnapshotBinding, build_chunk_index
    from pycode.engine import PyCodeEngine
    from pycode.graph_builder import build_code_graph
    from pycode.llm_client import LLMError
    from pycode.question import prepare_question
    from pycode.retrieval_evidence import RetrievalContext
    from pycode.semantic_search import VectorUnavailable
    from pycode.tools import TOOLS, ToolContext, ToolSpec
    from pycode.tools.base import failure, success
    from pycode.tools.read_file import read_file
    from pycode.tools.retrieve_context import retrieve_context

    root = Path(work).resolve()
    root.mkdir(parents=True, exist_ok=False)
    setup = perf_counter()
    manifest = {}
    for name in dataset['corpus_paths']:
        data = (Path(corpus) / name).read_bytes()
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        manifest[name] = sha(data)
    corpus_hash = sha(json.dumps(manifest, sort_keys=True).encode())
    binding = SnapshotBinding(str(uuid5(NAMESPACE_URL, 'pycode-a05-fixture-project')),
        str(uuid5(NAMESPACE_URL, dataset['corpus_commit'] + corpus_hash)), dataset['corpus_commit'])
    index = PyCodeEngine().build_index(root, include_chunk_metadata=True)
    graph = build_code_graph(index)
    trusted = RetrievalContext.create(root, index, graph, binding=binding,
        chunks=build_chunk_index(index, binding))
    requested = dict(dataset['limits'], **case.get('limits', {}))
    modern = (Path(implementation) / 'pycode/agent/budget.py').exists()
    now = [0.0]
    budget = None
    if modern:
        from pycode.agent.budget import ExecutionBudget, ExecutionLimits
        budget = ExecutionBudget(ExecutionLimits(**requested), binding=binding, clock=lambda: now[0])
        budget.retrieval_context = trusted
    counters = Counter()
    model_inputs = []
    small, large = dataset['corpus_paths']
    question = 'a05_missing_symbol' if case.get('preflight') else 'Explain ' + small
    final_text = 'Available evidence is insufficient.' if case['id'] in {
        'large_context', 'output_limit', 'upgrade_reuse'} else 'Inspect ' + small + ':1-2.'

    def operation(name):
        if name == 'final':
            return dict(action_type='final_answer', final_answer=final_text)
        if name.startswith('retrieve'):
            return dict(tool='retrieve_context', arguments={'question': question}, required=False)
        if name == 'search':
            return dict(tool='search_code', arguments={'pattern': 'a05_missing_symbol'}, required=False)
        if name == 'idle':
            return dict(tool='idle', arguments={}, required=False)
        path = large if name == 'large_optional' else 'missing.py' if name == 'missing_optional' else small
        return dict(tool='read_file', arguments={'file_path': path, 'max_chars': 1048576},
                    required=name == 'read')

    class Model:
        def generate(self, prompt):
            counters['model_calls'] += 1
            phase = ('plan' if prompt.startswith('You are the PyCode LLM Planner.') else
                     'next_action' if prompt.startswith('You are the PyCode LLM Next-Action Planner.') else 'summary')
            model_inputs.append(dict(phase=phase, utf8_bytes=len(prompt.encode('utf-8')),
                estimated_tokens=len(prompt.encode('utf-8')) + 512, sha256=sha(prompt.encode('utf-8'))))
            if phase == 'plan':
                if case['plan'] == 'timeout':
                    counters['model_failures'] += 1
                    raise LLMError('Synthetic timeout', category='timeout')
                if case['plan'] == 'invalid_json':
                    return 'Synthetic invalid JSON'
                return json.dumps([operation(name) for name in case['plan']])
            if phase == 'next_action':
                index = counters['decisions']
                counters['decisions'] += 1
                # An unexpected extra decision is a benchmark error, not success.
                if index >= len(case['actions']):
                    counters['script_overruns'] += 1
                    raise AssertionError('Unexpected extra model decision')
                value = operation(case['actions'][index])
                if 'tool' in value:
                    value = dict(action_type='tool_call', tool_name=value['tool'], arguments=value['arguments'])
                return json.dumps(value)
            return final_text

    def read(context, **arguments):
        counters['handler_attempts'] += 1
        mode = case.get('tool_mode')
        if mode == 'transient_once' and counters['handler_attempts'] == 1:
            raise OSError(errno.EAGAIN, 'Synthetic transient read')
        if mode == 'timeout':
            raise TimeoutError('Synthetic tool timeout')
        result = read_file(context, **arguments)
        if mode == 'deadline':
            now[0] += 2
        return result

    def retrieve(context, **arguments):
        counters['handler_attempts'] += 1
        return retrieve_context(context, **arguments)

    def search(context, **arguments):
        counters['handler_attempts'] += 1
        return failure('search_code', 'Synthetic unchanged failure', 'synthetic_failure')

    def vector_query(text, limit):
        counters['query_embedding_calls'] += 1
        if budget is not None:
            budget.consume('query_embeddings')
        raise VectorUnavailable('vector_provider_failed')

    tools = {'read_file': replace(TOOLS['read_file'], handler=read),
             'retrieve_context': replace(TOOLS['retrieve_context'], handler=retrieve),
             'search_code': replace(TOOLS['search_code'], handler=search),
             'idle': ToolSpec('idle', lambda context: success('idle', 'Synthetic idle'))}
    if case.get('preflight'):
        trusted = replace(trusted, retrieval_mode='vector', vector_query=vector_query)
        if budget is not None:
            budget.retrieval_context = trusted
    setup_ms = (perf_counter() - setup) * 1000
    started = perf_counter()
    with budget.activate() if budget is not None else nullcontext():
        preflight = prepare_question(question, dict(strategy='fast', intent='general', target=None), trusted) if case.get('preflight') else None
        seed = preflight.initial_evidence if preflight is not None else None
        result = run_agent_runtime(AgentTask(question, root, max_steps=requested['max_turns'], initial_evidence=seed),
            RuntimeConfig(max_turns=requested['max_turns'], enable_memory=False), tools=tools, llm_client=Model(),
            context=ToolContext(root, retrieval_context=trusted, initial_evidence=seed))
    elapsed_ms = (perf_counter() - started) * 1000
    observed = result.tool_results
    failed = sum(not value.ok for value in observed)
    completed = sum(value.ok for value in observed)
    # Shared observer metric; never infer execution success solely from answer text.
    required_operations = {}
    for turn in result.turns:
        if turn.tool_call is None or turn.tool_result is None:
            continue
        call = turn.tool_call
        if any(step.required and step.tool == call.name and step.arguments == call.arguments for step in result.steps):
            key = json.dumps([call.name, call.arguments], sort_keys=True)
            required_operations[key] = not turn.tool_result.ok
    required_failed = sum(required_operations.values())
    outcome = ('partial' if completed else 'failed') if (not result.ok or required_failed) else ('partial' if failed else 'complete')
    report = getattr(result, 'context_report', None)
    context_reports = budget.context_reports if budget is not None else []
    executed_context = [r for r in context_reports if r['delivered_to_model']]
    reasons = dict(sum((Counter(r['reason_counts']) for r in executed_context), Counter())) if modern else None
    events = result.trace.events if result.trace else []
    failures = Counter()
    repeated = 0
    for turn in result.turns:
        if turn.tool_call is not None and turn.tool_result is not None and not turn.tool_result.ok:
            signature = json.dumps([turn.tool_call.name, turn.tool_call.arguments,
                                   turn.tool_result.error, turn.tool_result.data], sort_keys=True)
            repeated += failures[signature] > 0
            failures[signature] += 1
    row = dict(case_id=case['id'], input_sha256=sha(json.dumps(case, sort_keys=True).encode()),
        config_sha256=sha(json.dumps(requested, sort_keys=True).encode()), binding=asdict(binding),
        corpus_sha256=corpus_hash, requested_limits=requested,
        effective_limits=asdict(budget.limits) if budget is not None else {'max_turns': requested['max_turns']},
        unsupported_limits=[] if modern else sorted(set(requested) - {'max_turns'}),
        latency_ms=elapsed_ms, setup_ms=setup_ms, simulated_elapsed_seconds=now[0],
        runtime_ok=result.ok, answer_present=bool(result.answer), outcome=outcome,
        failed_tool_observations=failed,
        stop_reason=str(result.stop_reason), tool_observations=len(observed), handler_attempts=counters['handler_attempts'],
        observations=[dict(tool=r.tool, ok=r.ok, path=r.data.get('path'),
            returned_content_chars=len(r.data['content']) if isinstance(r.data.get('content'), str) else None,
            content_sha256=sha(r.data['content'].encode()) if isinstance(r.data.get('content'), str) else None,
            execution=r.execution.to_dict() if getattr(r, 'execution', None) is not None else None) for r in observed],
        model_calls=counters['model_calls'], model_failures=counters['model_failures'],
        query_embedding_calls=counters['query_embedding_calls'], repeated_failed_observations=repeated,
        no_progress_events=sum(e.event_type == 'StopDecided' and e.data.get('stop_reason') == 'no_progress' for e in events),
        fallback_events=sum(e.event_type in {'LLMPlanFallback', 'LLMNextActionFallback'} for e in events),
        model_inputs=model_inputs, estimated_input_tokens=sum(p['estimated_tokens'] for p in model_inputs),
        peak_estimated_input_tokens=max((p['estimated_tokens'] for p in model_inputs), default=0),
        prompt_within_budget=all(p['estimated_tokens'] <= requested['context_tokens'] for p in model_inputs),
        discard_reasons=reasons, actual_context=report, verification=getattr(result, 'verification', None),
        execution_budget=getattr(result, 'execution_budget', None),
        execution_report=getattr(result, 'execution_report', None),
        pack_reused=any(r.data.get('initial_evidence_reused') is True for r in observed),
        pack_digest=seed.digest if seed is not None else None,
        preflight_status=preflight.status if preflight is not None else None,
        preflight_summary=preflight.summary() if preflight is not None else None,
        real_model_calls=0, real_cost=None, billed_tokens=None, transport_attempts=None,
        natural_language_quality=None)
    row['contract_errors'] = contract_errors(row, dict(dataset.get('invariants', {}), **case['expected']))
    if counters['script_overruns']:
        row['contract_errors'].append('script_overrun')
    row['contract_passed'] = not row['contract_errors']
    return row


def main():
    implementation, dataset_path, corpus, output, work, repeats, warmups = sys.argv[1:]
    implementation = Path(implementation).resolve()
    # Remove current-worktree import fallback and prove the selected package path.
    repository = Path(__file__).resolve().parents[2]
    sys.path = [str(implementation)] + [p for p in sys.path if p and Path(p).resolve() != repository]
    block_network()
    import pycode
    if Path(pycode.__file__).resolve() != implementation / 'pycode/__init__.py':
        raise RuntimeError('Wrong PyCode implementation imported')

    dataset = json.loads(Path(dataset_path).read_text(encoding='utf-8'))
    rows = []
    for sample in range(-int(warmups), int(repeats)):
        for case in dataset['cases']:
            row = run_case(dataset, case, corpus, Path(work) / f'{sample}_{case["id"]}', implementation)
            rows.append(dict(row, sample=sample, warmup=sample < 0))
    value = dict(schema_version=1, implementation=str(implementation), imported_package=str(pycode.__file__), rows=rows)
    path = Path(output)
    with path.open('x', encoding='utf-8') as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')


if __name__ == '__main__':
    main()
