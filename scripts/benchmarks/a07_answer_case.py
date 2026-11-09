"""Isolated A07b execution adapter. Calls real Engine/Adapter/Runtime, no Loop copy.

Only the explicitly exported PyCode implementation is imported in a subprocess.
The analysed corpus is copied/read, never imported. Fake policy sees prompts only.
"""
from __future__ import annotations

from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import socket
import sys
from time import perf_counter
from unittest.mock import patch


def sha(data):
    return hashlib.sha256(data).hexdigest()


def block_network():
    def forbidden(*args, **kwargs):
        raise AssertionError('A07b forbids network; use the explicit Fake clients')
    return (patch.object(socket.socket, 'connect', forbidden),
            patch.object(socket, 'create_connection', forbidden),
            patch.object(socket, 'getaddrinfo', forbidden))


def visible_code(prompt):
    """Observe ONLY source lines actually serialized into this model request."""
    items = []
    decoder = json.JSONDecoder()
    if prompt.startswith('You are the PyCode LLM Planner.'):
        payload = json.loads(prompt.split('Planning input:\n', 1)[1])
        items.extend(payload.get('initial_evidence', {}).get('items', []))
    elif prompt.startswith('You are the PyCode LLM Next-Action Planner.'):
        payload = json.loads(prompt.split('Next-action input:\n', 1)[1])
        for section in payload['context_sections']:
            for match in re.finditer(r'\nData:\n', section['content']):
                value, _ = decoder.raw_decode(section['content'][match.end():])
                item = value.get('item', {})
                if item.get('snippet') and item.get('path'):
                    items.append(item)
    else:
        for block in re.split(r'^## 证据 \d+: ',prompt,flags=re.M)[1:]:
            path = re.search(r'^路径: (.+)$', block, re.M)
            if path and '\n代码片段:\n' in block:
                items.append(dict(path=path[1], snippet=block.split('\n代码片段:\n', 1)[1]))
    observed = []
    for item in items:
        lines = []
        for line in item['snippet'].splitlines():
            number, separator, text = line.partition(': ')
            if separator and number.isdecimal():
                lines.append([int(number), text])
        if lines:
            observed.append(dict(path=item['path'], lines=lines,
                start_line=lines[0][0], end_line=lines[-1][0]))
    return observed


class ScriptedModel:
    """No dataset, GT, source tree or hidden tool result is available to this policy."""
    def __init__(self, question):
        self.question = question
        self.calls = []

    def generate(self, prompt):
        phase = ('selection' if prompt.startswith('Select project memories') else
                 'extraction' if prompt.startswith('Extract durable PyCode memories') else
                 'plan' if prompt.startswith('You are the PyCode LLM Planner.') else
                 'next_action' if prompt.startswith('You are the PyCode LLM Next-Action Planner.') else 'answer')
        visible = visible_code(prompt) if phase in {'answer', 'next_action','plan'} else []
        call = dict(phase=phase, prompt=prompt, response=None, failed=False,
            prompt_sha256=sha(prompt.encode()), input_utf8_bytes=len(prompt.encode()),
            estimated_input_tokens=len(prompt.encode())+512,
            output_utf8_bytes=0, visible_code=visible)
        self.calls.append(call)
        try:
            response = self._respond(phase, prompt, visible)
        except Exception:
            call['failed'] = True
            raise
        call.update(response=response,output_utf8_bytes=len(response.encode()))
        return response

    def _respond(self, phase, prompt, visible):
        if phase == 'selection':
            catalog = json.loads(prompt.split('Memory catalog:\n\n', 1)[1])
            return json.dumps([v['id'] for v in catalog[:1]])
        if phase == 'extraction':
            return '[]'
        if phase == 'plan':
            return json.dumps([dict(tool='retrieve_context', arguments={'question':self.question}, required=True)])
        if phase == 'next_action':
            payload = json.loads(prompt.split('Next-action input:\n', 1)[1])
            count = payload['turn']['tool_results']
            if count == 0:
                return json.dumps(dict(action_type='tool_call', tool_name='retrieve_context',
                                      arguments={'question':self.question}))
            if count == 1 and visible:
                return json.dumps(dict(action_type='tool_call', tool_name='query_graph',
                    arguments={'query_type':'imports', 'target':visible[0]['path']}))
        addresses = list(dict.fromkeys(f"{v['path']}:{v['start_line']}" for v in visible))[:4]
        answer = ('FAKE engineering placeholder; no factual answer evaluated. Visible addresses: '
                  + ', '.join(addresses)) if addresses else 'FAKE engineering placeholder; visible source is insufficient.'
        return json.dumps(dict(action_type='final_answer', final_answer=answer)) if phase == 'next_action' else answer


def effective_controls(protocol, variant):
    if variant not in protocol['variants']:
        raise ValueError('Unknown A07b variant')
    limits = dict(protocol['limits'])
    controls = dict(enable_memory=True, graph_depth=protocol['retrieval']['graph_depth'],
                    vector_candidates=True, keyword_candidates=True)
    if variant == 'memory_off': controls['enable_memory'] = False
    if variant == 'one_turn': limits['max_turns'] = 1
    if variant == 'graph_off': controls['graph_depth'] = 0
    if variant == 'vector_off': controls['vector_candidates'] = False
    if variant == 'keyword_off': controls['keyword_candidates'] = False
    return dict(limits=limits, **controls)


def run_current(protocol, public_case, context, root, variant, *, graph_path=None, checkpoint=lambda:None):
    """Trusted context/controls are server-side Python values, never Tool parameters."""
    from backend.app.integrations.pycode import PyCodeAdapter
    from pycode.agent.budget import ExecutionLimits
    from pycode.engine import PyCodeEngine
    from pycode import retriever
    from pycode.semantic_search import VectorResult

    root = Path(root).resolve()
    controls = effective_controls(protocol, variant)
    limits = ExecutionLimits(**controls['limits'])
    trusted = replace(context, root=root, graph_depth=controls['graph_depth'])
    if not controls['vector_candidates']:
        trusted = replace(trusted, vector_query=lambda text, limit:VectorResult(context.binding, ()))
    model = ScriptedModel(public_case['question'])

    class Engine(PyCodeEngine):
        def run_agent(self, *args, **kwargs):
            return super().run_agent(*args, **kwargs, enable_memory=controls['enable_memory'],
                enable_memory_extraction=controls['enable_memory'], graph_path=graph_path)

    adapter = PyCodeAdapter(engine=Engine(), llm_client=model)
    decision = asdict(adapter.decide_question(public_case['question'], public_case['target']))
    # Controlled paired paths, NOT a production route choice or a public API override.
    decision['strategy'] = 'fast'
    budget = None; result = None; preflight = None; error = None
    with ExitStack() as stack:
        for guard in block_network(): stack.enter_context(guard)
        stack.enter_context(patch.object(ExecutionLimits, 'from_environment', return_value=limits))
        if not controls['keyword_candidates']:
            stack.enter_context(patch.object(retriever, '_lexical_candidates', return_value=[]))
        started = perf_counter()
        try:
            with adapter.execution_scope(trusted, checkpoint, run_id=protocol['seed_run_id']) as budget:
                preflight = adapter.prepare_question(public_case['question'], decision, trusted)
                result = (adapter.answer_question(root, public_case['question'], decision=decision,
                    retrieval_context=trusted, preflight=preflight) if variant == 'fast' else
                    adapter.run_agent(root, public_case['question'], retrieval_context=trusted, preflight=preflight))
                adapter.record_budget(result)
        except Exception as exc:
            error = dict(type=type(exc).__name__, code=getattr(exc, 'code', None), message=str(exc))
        latency = (perf_counter()-started)*1000
    tools = getattr(result, 'tool_results', [])
    answer = getattr(result, 'answer', None)
    ok = result is not None and getattr(result, 'ok', True) and error is None
    execution = getattr(result, 'execution_report', None)
    row = dict(variant=variant, path=protocol['variants'][variant]['path'],
        classification='current_fake_engineering', question_id=public_case['id'],
        binding=asdict(trusted.binding), trusted_runtime_binding=True, controls=controls,
        runtime_ok=bool(ok), outcome=('failed' if not ok else 'partial' if any(not t.ok for t in tools) else 'complete'),
        outcome_scope='engineering_task_completion_not_human_quality',
        harness_outcome=execution['outcome'] if execution else None,
        stop_reason=str(getattr(result, 'stop_reason', None) or (budget.stop_reason if budget else None)
                        or ('fast_return' if ok else 'not_started')),
        error=error, answer=answer, latency_ms=latency,
        calls=model.calls, model_calls=len(model.calls), model_failures=sum(c['failed'] for c in model.calls),
        model_input_estimated_tokens=sum(c['estimated_input_tokens'] for c in model.calls),
        output_utf8_bytes=sum(c['output_utf8_bytes'] for c in model.calls),
        peak_estimated_input_tokens=max((c['estimated_input_tokens'] for c in model.calls), default=0),
        tool_calls=len(tools), tool_successes=sum(t.ok for t in tools), tool_failures=sum(not t.ok for t in tools),
        handler_attempts=sum(t.execution.attempts if t.execution else 1 for t in tools),
        tools=[dict(tool=t.tool, ok=t.ok, data=t.data, error=t.error,
                    execution=t.execution.to_dict() if t.execution else None) for t in tools],
        trace=[dict(event_type=e.event_type,status=e.status,data=e.data) for e in getattr(getattr(result,'trace',None),'events',[])],
        execution_report=execution, execution_budget=budget.snapshot() if budget else None,
        context_reports=deepcopy(budget.context_reports) if budget else [],
        verification=getattr(result,'verification',None),
        preflight=preflight.summary() if preflight else None,
        memory=getattr(result,'memory',None).public_summary() if getattr(result,'memory',None) else None,
        initial_pack_reused=any(t.data.get('initial_evidence_reused') for t in tools),
        real_model_calls=0, billed_tokens=None, real_cost=None, quality={k:None for k in protocol['quality']['dimensions']})
    return row


def run_legacy(public_case, root):
    from pycode.engine import PyCodeEngine
    model = ScriptedModel(public_case['question'])
    started = perf_counter()
    try:
        result = (PyCodeEngine().impact(root, public_case['target'], llm_client=model)
                  if public_case['mode']=='impact' else
                  PyCodeEngine().ask(root, public_case['question'], llm_client=model))
        answer, error, ok = result.answer, None, True
    except Exception as exc:
        answer, error, ok = None, dict(type=type(exc).__name__, message=str(exc)), False
    return dict(variant='legacy',path='legacy',classification='substitute_legacy_sync_not_original_A00',
        question_id=public_case['id'], trusted_runtime_binding=False, runtime_ok=ok,
        outcome='complete' if ok else 'failed', stop_reason='sync_return' if ok else 'error',
        error=error, answer=answer, latency_ms=(perf_counter()-started)*1000,
        calls=model.calls, model_calls=len(model.calls),model_failures=sum(c['failed'] for c in model.calls),
        model_input_estimated_tokens=sum(c['estimated_input_tokens'] for c in model.calls),
        output_utf8_bytes=sum(c['output_utf8_bytes'] for c in model.calls),
        peak_estimated_input_tokens=max((c['estimated_input_tokens'] for c in model.calls),default=0),
        tool_calls=0,tool_successes=0,tool_failures=0,handler_attempts=0,tools=[],trace=[],
        context_reports=[],execution_report=None,execution_budget=None,verification=None,
        preflight=None,memory=None,initial_pack_reused=False,real_model_calls=0,billed_tokens=None,real_cost=None,
        original_A00_reproduced=False)


def main():
    # Freeze import precedence before importing any PyCode or backend code.
    implementation, inputs, output, variant_family = map(Path, sys.argv[1:5])
    repository = Path(__file__).resolve().parents[2]
    sys.path.insert(0,str(implementation.resolve()))
    sys.dont_write_bytecode = True
    protocol=json.loads((inputs/'protocol.json').read_text(encoding='utf-8'))
    public=json.loads((inputs/'public-questions.json').read_text(encoding='utf-8'))
    output.mkdir(exist_ok=False)
    paths=[]
    def save(row):
        name=f'case-{len(paths):04d}.json'
        (output/name).write_text(json.dumps(row,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
        paths.append(name)
        (output/'rows.json').write_text(json.dumps(paths,indent=2)+'\n',encoding='utf-8')
    with ExitStack() as guards:
        for guard in block_network(): guards.enter_context(guard)
        if str(variant_family)=='legacy':
            from pycode.engine import PyCodeEngine
            from pycode.graph_builder import build_code_graph
            from pycode.storage import save_graph
            root=output/'corpus';shutil.copytree(inputs/'corpus',root)
            engine=PyCodeEngine();index=engine.index(root)
            from pycode.constants import DEFAULT_ARTIFACT_DIR,DEFAULT_GRAPH_FILE
            save_graph(build_code_graph(index),root/DEFAULT_ARTIFACT_DIR/DEFAULT_GRAPH_FILE)
            for repeat in range(protocol['repeats']):
                for case in public:
                    row=run_legacy(case,root);row['repeat']=repeat;save(row)
        else:
            # Backend and benchmark helpers resolve from repository; PyCode is
            # already fixed to the exported current implementation above.
            sys.path.append(str(repository))
            from pycode.chunks import SnapshotBinding,load_chunk_index
            from pycode.storage import load_index,load_graph
            from pycode.retrieval_evidence import RetrievalContext
            from pycode.semantic_index import SemanticIdentity,file_hash
            from pycode.semantic_search import ReadOnlyVectorIndex
            from pycode.agent.budget import current_budget
            from pycode.agent.memory import MemoryStore
            from pycode.agent.memory_record import MemoryScope
            from scripts.benchmarks.a02_retrieval import HashFakeProvider
            from scripts.benchmarks.a06_memory import add,storage
            receipts=json.loads((inputs/'a07a-results.json').read_text(encoding='utf-8'))
            bound=SnapshotBinding(**receipts['retrievals']['131072']['binding'])
            chunks=load_chunk_index(inputs/'chunks.json',bound)
            base=RetrievalContext.create(inputs/'corpus',load_index(inputs/'index.json'),load_graph(inputs/'graph.json'),binding=bound,chunks=chunks)
            provider=HashFakeProvider()
            vector=ReadOnlyVectorIndex.load(inputs/'fake-artifact',SemanticIdentity(bound,file_hash(inputs/'chunks.json'),provider.provider,provider.model,provider.dimension),chunks)
            def query(text,limit):
                current_budget().consume('query_embeddings')
                return vector.search(provider.embed_query(text),limit)
            base=replace(base,retrieval_mode='hybrid',vector_top_k=protocol['retrieval']['top_k'],token_budget=protocol['retrieval']['token_budget'],vector_query=query)
            seed=output/'memory-seed';seed.mkdir()
            with patch('pycode.agent.memory.utc_now',return_value=datetime.fromisoformat(protocol['clock'])):
                add(MemoryStore(seed,scope=MemoryScope(seed,bound,protocol['seed_run_id']),origin='user_explicit'),protocol['memory_note'])
            # One immutable corpus, serial tasks. Only this new experiment's
            # synthetic Memory files are restored; no user/legacy data is read.
            root=output/'work'/'frozen-workspace'
            shutil.copytree(inputs/'corpus',root)
            shutil.copytree(seed/'.pclens',root/'.pclens')
            shutil.copyfile(inputs/'index.json',root/'.pclens/index.json')
            memory_seed={p.relative_to(seed/'.pclens/memory'):p.read_bytes()
                         for p in (seed/'.pclens/memory').iterdir() if p.is_file()}
            source_hashes={p.relative_to(inputs/'corpus'):sha(p.read_bytes())
                           for p in (inputs/'corpus').rglob('*.py')}
            for repeat in range(protocol['repeats']):
                for case in public:
                    variants=['fast','agent']+([v for v in protocol['variants'] if v not in {'fast','agent','legacy'}] if case['id'] in protocol['ablation_case_ids'] else [])
                    shift=(repeat+int(case['id'][1:]))%len(variants);variants=variants[shift:]+variants[:shift]
                    for variant in variants:
                        memory_dir=root/'.pclens/memory'
                        if {p.name for p in memory_dir.iterdir() if p.is_file()}!={p.name for p in memory_seed}:
                            raise ValueError('Unexpected synthetic Memory records; refuse implicit cleanup')
                        for name,data in memory_seed.items():(memory_dir/name).write_bytes(data)
                        before={k:sha(v) for k,v in storage(root).items()}
                        row=run_current(protocol,case,base,root,variant,graph_path=inputs/'graph.json')
                        row.update(repeat=repeat,memory_before=before,memory_after={k:sha(v) for k,v in storage(root).items()})
                        row['workspace_setup']='shared_frozen_corpus_reset_synthetic_memory_v2'
                        row['binding']=asdict(bound);save(row)
                        print(json.dumps(dict(case=case['id'],variant=variant,repeat=repeat,outcome=row['outcome'])),flush=True)
            if any(sha((root/name).read_bytes())!=digest for name,digest in source_hashes.items()):
                raise ValueError('Frozen experiment source changed')


if __name__=='__main__':main()
