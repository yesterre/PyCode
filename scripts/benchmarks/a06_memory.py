"""Fixed A06 engineering scenarios, real Runtime, Fake models, no network.

python -m scripts.benchmarks.a06_memory --output-dir <new-directory>
Only creates synthetic workspaces under that new directory; never opens .env
or existing Memory. Token figures use A05's conservative estimator, not billing.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import csv
import hashlib
import json
import math
from pathlib import Path
import platform
import re
import statistics
import subprocess
import time
from unittest.mock import patch

from pycode.agent.budget import ExecutionBudget, ExecutionLimits, current_budget
from pycode.agent.context_budget import ContextUnit, estimate
from pycode.agent.memory import MemoryStore, format_relevant_memories
from pycode.agent.memory_record import MemoryScope
from pycode.agent.runtime import run_agent_runtime
from pycode.agent.types import AgentTask, RuntimeConfig
from pycode.chunks import SnapshotBinding, build_chunk_index
from pycode.engine import PyCodeEngine
from pycode.graph_builder import build_code_graph
from pycode.retrieval_evidence import RetrievalContext
from pycode.tools import ToolContext, ToolSpec, retrieve_context


ROOT=Path(__file__).resolve().parents[2]
FIXTURE=ROOT/'tests/fixtures/a06/memory_cases.json'
FIXTURE_SHA256='aa25ebd515f6a059358834e881976c61d0cc1455889d4948340c3ed8ee657da8'
MODES=('enabled','disabled')


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read_fixture(path=FIXTURE):
    value=json.loads(Path(path).read_text(encoding='utf-8'))
    checksum=sha(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())
    if checksum!=FIXTURE_SHA256:
        raise ValueError('Fixed A06 fixture changed; review and version explicitly')
    ids=[case['id'] for case in value['cases']]
    if (value['schema_version']!=1 or value['kind']!='offline_memory_engineering_not_model_quality'
            or len(ids)!=len(set(ids)) or any(not re.fullmatch('[a-z_]+',i) for i in ids)):
        raise ValueError('Invalid A06 scenario contract')
    return value,checksum


def note(dataset, overrides):
    value={**dataset['note'],**{k:v for k,v in overrides.items() if k in dataset['note']}}
    if 'body_repeat' in overrides:
        repeat=overrides['body_repeat']
        value['body']=repeat['text']*repeat['count']
    return value


def add(store, value):
    return store.add_memory(memory_id=value['id'],memory_type=value['type'],title=value['title'],
        summary=value['summary'],body=value['body'],tags=value['tags'],related_files=value['related_files'])


def storage(root):
    return {p.name:p.read_bytes() for p in (root/'.pclens/memory').glob('*.md')
            if p.name.casefold()!='memory.md'}


class FakeModel:
    def __init__(self, case, dataset):
        self.case,self.dataset=case,dataset
        self.calls=[]
        self.source_priority_observations=[]

    def generate(self, prompt):
        purpose=('selection' if prompt.startswith('Select project memories') else
                 'extraction' if prompt.startswith('Extract durable PyCode memories') else 'analysis')
        self.calls.append(dict(purpose=purpose,estimated_tokens=estimate(prompt),
            current_source_present='CURRENT_SOURCE' in prompt,failed=False))
        if self.case.get('provider_failure') and purpose in {'selection','extraction'}:
            self.calls[-1]['failed']=True
            raise TimeoutError('Synthetic A06 provider failure')
        if purpose=='selection':
            catalog=json.loads(prompt.split('Memory catalog:\n\n')[-1])
            return json.dumps([entry['id'] for entry in catalog[:5]])
        if purpose=='extraction':
            return json.dumps([note(self.dataset,p) for p in self.case.get('proposals',[])])
        if prompt.startswith('You are the PyCode LLM Planner.'):
            return json.dumps([dict(tool='retrieve_context',arguments={'question':'entry'},required=True)])
        if prompt.startswith('You are the PyCode LLM Next-Action Planner.'):
            payload=json.loads(prompt.split('Next-action input:\n')[-1])
            turn=payload['turn']['turn_index']
            if self.case['id']=='source_priority' and payload['turn']['tool_results']:
                report=current_budget().context_reports[-1]
                sections=payload['context_sections']
                items=[json.loads(s['content'].split('\nData:\n',1)[1])['item']
                       for s in sections if s['name']=='tool_results']
                snippet='\n'.join(f'{n}: {line}' for n,line in enumerate(self.dataset['source'].splitlines(),1))
                self.source_priority_observations.append(dict(
                    delivered=report['delivered_to_model'],
                    binding_matches=report['binding']==payload['task']['binding']==self.dataset['binding'],
                    whole_source_present=any(item.get('snippet')==snippet and item.get('path')=='entry.py'
                        and item.get('start_line')==1 and item.get('end_line')==2 and item.get('chunk_ids')
                        and all(item.get(k)==v for k,v in self.dataset['binding'].items()) for item in items),
                    memory_section_absent=all(s['name']!='memory' for s in sections),
                    memory_fragment_absent='OLD_HINT' not in prompt,
                    budget_discard_ids=[d['id'] for d in report['decisions']
                        if d['reason']=='context_budget' and d['reference'].get('source')=='memory_hint']))
            if turn==1:
                return json.dumps(dict(action_type='tool_call',tool_name='retrieve_context',arguments={'question':'entry'}))
            return json.dumps(dict(action_type='final_answer',final_answer='entry.py returns CURRENT_SOURCE.'))
        return 'entry.py returns CURRENT_SOURCE.'


def run_case(dataset, case, root, mode):
    if mode not in MODES:
        raise ValueError('Invalid Memory mode')
    root=Path(root)
    root.mkdir(parents=True,exist_ok=False)
    setup_start=time.perf_counter()
    binding=SnapshotBinding(**dataset['binding'])
    scope=MemoryScope(root,binding,dataset['run_id'])
    (root/'entry.py').write_text(dataset['source'],encoding='utf-8')
    index=PyCodeEngine().build_index(root,include_chunk_metadata=True)
    chunks=build_chunk_index(index,binding)
    context=RetrievalContext.create(root,index,build_code_graph(index),binding=binding,chunks=chunks)
    ref=dict(path='entry.py',start_line=1,end_line=2,chunk_ids=[c.id for c in chunks.chunks])
    limits=ExecutionLimits(**{**dataset['limits'],'context_tokens':case.get('context_tokens',dataset['limits']['context_tokens'])})
    model=FakeModel(case,dataset)
    original_save=MemoryStore._save_memory
    with ExitStack() as stack:
        # Deterministic record times, synthetic failure/capacity injection only.
        clock=stack.enter_context(patch('pycode.agent.memory.utc_now',return_value=datetime.fromisoformat(dataset['clock'])))
        if 'record_limit' in case:
            stack.enter_context(patch('pycode.agent.memory.MAX_RECORDS',case['record_limit']))
        stack.enter_context(patch('socket.socket.connect',side_effect=AssertionError('A06 benchmark forbids network')))
        stack.enter_context(patch('socket.create_connection',side_effect=AssertionError('A06 benchmark forbids network')))
        records={}
        for step in case['setup']:
            which=step.get('binding','current')
            bound=(replace(binding,snapshot_id='20000000-0000-4000-8000-000000000002') if which=='old_snapshot'
                   else replace(binding,project_id='10000000-0000-4000-8000-000000000002') if which=='foreign_project' else binding)
            origin=step.get('origin','model_inference')
            store=MemoryStore(root,scope=MemoryScope(root,bound,dataset['run_id']),origin=origin,
                evidence=(ref,) if origin=='code_derived' else (),retrieval_context=context)
            operation=step['op']
            if operation=='add':
                item=add(store,note(dataset,step));records[step.get('id','note')]=item
            elif operation in {'delete','invalidate'}:
                store.manage_memory(records[step['ref']].id,operation)
            elif operation=='damage_provenance':
                path=store.memory_dir/records[step['ref']].path
                path.write_text('\n'.join('provenance: null' if line.startswith('provenance:') else line
                                          for line in path.read_text(encoding='utf-8').split('\n')),encoding='utf-8')
            elif operation=='bad_record':
                (store.memory_dir/'broken.md').write_bytes(b'\xff')
            elif operation=='bad_index':
                store.index_path.write_text('Synthetic broken derived index',encoding='utf-8')
            elif operation=='rebuild':
                store.rebuild_index()
            else:
                raise ValueError('Unsupported fixture setup operation')
        clock.return_value+=timedelta(days=case.get('advance_days',0))
        before=storage(root)
        setup_ms=(time.perf_counter()-setup_start)*1000
        budget=ExecutionBudget(limits,binding=binding);budget.retrieval_context=context
        def retrieve(tool_context,question):
            # "entry" alone is a special intent, not proof this synthetic
            # function is a runtime entrypoint. Read the fixed explicit path.
            result=retrieve_context(tool_context,question,intent='explain',target='entry.py')
            if 'tool_summary' in case:
                result.summary=case['tool_summary']
            return result
        def fail_save(store,item):
            raise OSError('Synthetic A06 write failure')
        started=time.perf_counter()
        with scope.activate(),budget.activate(),patch.object(MemoryStore,'_save_memory',
                fail_save if case.get('write_failure') else original_save):
            result=run_agent_runtime(AgentTask('Explain entry',root),RuntimeConfig(enable_memory=mode=='enabled',
                execution_limits=limits),llm_client=model,
                tools={'retrieve_context':ToolSpec('retrieve_context',retrieve)},
                context=ToolContext(root,retrieval_context=context))
        latency_ms=(time.perf_counter()-started)*1000
        after_runtime=storage(root)
        # Explicit post-failure recovery, never part of the measured Runtime.
        recovered=False
        if case.get('recover') and mode=='enabled':
            add(MemoryStore(root,scope=scope),note(dataset,dict(id='recovered',title='Recovery check',body='Recovery write succeeds.')))
            recovered=True
        store=MemoryStore(root,scope=scope)
        available=sorted(e.id for e in store.list_memories())
        after=storage(root)
        info=result.memory
        summary=info.public_summary() if info is not None else None
        selected=[m.id for m in info.relevant_memories] if info is not None else []
        created=len(info.extracted_memories) if info is not None else 0
        memory_decisions=Counter(d['reason'] for r in budget.context_reports for d in r['decisions']
                                 if d['reference'].get('source')=='memory_hint')
        analysis=[c for c in model.calls if c['purpose']=='analysis']
        memory_unit_ids=[ContextUnit('Memory hint; current Snapshot code takes precedence.\n'+format_relevant_memories([m]),
                                    {'source':'memory_hint'}).id for m in info.relevant_memories] if info and case['id']=='source_priority' else []
        priority=model.source_priority_observations
        row=dict(case_id=case['id'],mode=mode,binding=dataset['binding'],synthetic_binding=True,
            runtime_ok=result.ok,stop_reason=str(result.stop_reason),selected_ids=selected,created_count=created,
            available_ids=available,reason_counts=summary['reason_counts'] if summary else {},
            source_counts=summary['source_counts'] if summary else {},diagnostics=store.diagnostics,
            selection_error=bool(info and info.selection_error),extraction_error=bool(info and info.extraction_error),
            calls=dict(Counter(c['purpose'] for c in model.calls)),model_failures=sum(c['failed'] for c in model.calls),
            model_calls=len(model.calls),execution_model_calls=budget.usage['model_calls'],
            estimated_input_tokens=sum(c['estimated_tokens'] for c in model.calls),
            memory_estimated_input_tokens=sum(c['estimated_tokens'] for c in model.calls if c['purpose']!='analysis'),
            peak_estimated_input_tokens=max((c['estimated_tokens'] for c in model.calls),default=0),
            context_budget=limits.context_tokens,memory_context_decisions=dict(memory_decisions),
            source_priority_observations=priority,
            memory_whole_omission_proved=bool(memory_unit_ids and priority) and all(
                set(memory_unit_ids)<=set(o['budget_discard_ids']) and o['memory_section_absent']
                and o['memory_fragment_absent'] for o in priority),
            current_source_delivered=any(c['current_source_present'] for c in analysis),
            context_source_references=sum(ref.get('source')=='snapshot_source'
                for report in budget.context_reports for ref in report['references']),
            preserved_existing_records=all(after_runtime.get(name)==data for name,data in before.items()),
            disabled_store_unchanged=before==after_runtime if mode=='disabled' else None,
            storage_records_before=len(before),storage_records_after=len(after),
            storage_bytes_before=sum(map(len,before.values())),storage_bytes_after=sum(map(len,after.values())),
            derived_index_bytes=store.index_path.stat().st_size if store.index_path.exists() else 0,
            recovered=recovered,scaled_record_limit=case.get('record_limit'),
            setup_ms=setup_ms,latency_ms=latency_ms,real_model_calls=0,
            billed_tokens=None,real_cost=None,natural_language_quality=None)
        row['contract_errors']=check_contract(row,case)
        row['contract_passed']=not row['contract_errors']
        return row


def check_contract(row,case):
    errors=[]
    def require(ok,name):
        if not ok:errors.append(name)
    require(row['runtime_ok'],'runtime_failed')
    require(row['current_source_delivered'] and row['context_source_references']>0,'source_not_delivered')
    require(row['preserved_existing_records'],'old_record_modified')
    require(row['peak_estimated_input_tokens']<=row['context_budget'],'prompt_over_budget')
    require(row['model_calls']==row['execution_model_calls'],'model_accounting')
    if case['id']=='source_priority':
        require(bool(row['source_priority_observations']) and all(o['delivered'] and o['binding_matches']
            and o['whole_source_present'] for o in row['source_priority_observations']),'bound_source_not_delivered')
    if row['mode']=='disabled':
        require(not row['selected_ids'] and not row['created_count'],'disabled_memory_used')
        require(not row['calls'].get('selection') and not row['calls'].get('extraction'),'disabled_memory_called')
        require(row['disabled_store_unchanged'],'disabled_store_mutated')
    else:
        expected=case['expected']
        for key,actual in (('selected',row['selected_ids']),('created',row['created_count']),('available',row['available_ids'])):
            require(expected[key]==actual,key+'_mismatch')
        for reason in expected.get('reasons',[]):require(row['reason_counts'].get(reason,0)>0,'missing_reason_'+reason)
        for code in expected.get('diagnostics',[]):require(code in row['diagnostics'],'missing_diagnostic_'+code)
        require(row['extraction_error']==expected.get('extraction_error',False),'extraction_error_mismatch')
        if 'origin' in expected:require(row['source_counts']=={expected['origin']:len(row['selected_ids'])},'origin_mismatch')
        if expected.get('memory_context_omitted'):
            require(row['memory_context_decisions'].get('context_budget',0)>0,'memory_not_omitted_by_context_budget')
            require(row['memory_whole_omission_proved'],'whole_memory_omission_not_proved')
        if case.get('recover'):require(row['recovered'],'recovery_failed')
    return errors


def distribution(values):
    ordered=sorted(values)
    return dict(samples=len(ordered),p50=statistics.median(ordered),p95=ordered[math.ceil(.95*len(ordered))-1],
                mean=statistics.mean(ordered))


def summarize(rows):
    return dict(samples=len(rows),contract_passed=sum(r['contract_passed'] for r in rows),
        runtime_ok=sum(r['runtime_ok'] for r in rows),
        model_calls=sum(r['model_calls'] for r in rows),
        selection_calls=sum(r['calls'].get('selection',0) for r in rows),
        extraction_calls=sum(r['calls'].get('extraction',0) for r in rows),
        selected=sum(len(r['selected_ids']) for r in rows),created=sum(r['created_count'] for r in rows),
        reason_counts=dict(sum((Counter(r['reason_counts']) for r in rows),Counter())),
        source_counts=dict(sum((Counter(r['source_counts']) for r in rows),Counter())),
        latency_ms=distribution([r['latency_ms'] for r in rows]),
        setup_ms=distribution([r['setup_ms'] for r in rows]),
        estimated_input_tokens=distribution([r['estimated_input_tokens'] for r in rows]),
        memory_estimated_input_tokens=sum(r['memory_estimated_input_tokens'] for r in rows),
        storage_bytes_after=distribution([r['storage_bytes_after'] for r in rows]))


def run_benchmark(output, *, repeats=3, warmups=1):
    if type(repeats)is not int or not 1<=repeats<=20 or type(warmups)is not int or not 0<=warmups<=3:
        raise ValueError('Invalid repeat/warmup count')
    dataset,checksum=read_fixture()
    out=Path(output).resolve();out.mkdir(parents=True,exist_ok=False)
    git=lambda *args:subprocess.check_output(['git',*args],cwd=ROOT,text=True).strip()
    entry_head=git('rev-parse','HEAD')
    paths=git('ls-files','--','pycode','backend').splitlines()
    manifest=lambda:{name:sha((ROOT/name).read_bytes()) for name in paths if (ROOT/name).is_file()}
    source_hashes=manifest()
    driver_hash=sha(Path(__file__).read_bytes())
    rows=[];started=datetime.now(timezone.utc).isoformat()
    for mode in MODES:
        for case in dataset['cases']:
            for trial in range(warmups+repeats):
                row=run_case(dataset,case,out/'work'/mode/case['id']/str(trial),mode)
                row.update(trial=trial,warmup=trial<warmups)
                rows.append(row)
    measured=[r for r in rows if not r['warmup']]
    unchanged=(manifest()==source_hashes and git('rev-parse','HEAD')==entry_head
               and sha(Path(__file__).read_bytes())==driver_hash)
    if not unchanged:
        for row in rows:
            row['contract_errors'].append('implementation_changed')
            row['contract_passed']=False
    report=dict(schema_version=1,kind=dataset['kind'],started_at_utc=started,
        finished_at_utc=datetime.now(timezone.utc).isoformat(),head=entry_head,
        branch=git('branch','--show-current'),contract_commit=dataset['contract_commit'],
        fixture_sha256=checksum,corpus_sha256=sha(dataset['source'].encode()),driver_sha256=driver_hash,
        implementation_unchanged=unchanged,
        implementation_hashes=source_hashes,dirty_system_paths=git('status','--short','--','pycode','backend'),
        environment=dict(python=platform.python_version(),platform=platform.platform(),network='blocked',models='Fake'),
        estimator='utf8_bytes_upper_bound_v1',billed_tokens=None,real_cost=None,natural_language_quality=None,
        repeats=repeats,warmups=warmups,
        summaries={mode:summarize([r for r in measured if r['mode']==mode]) for mode in MODES},
        cases={case['id']:{mode:summarize([r for r in measured if r['mode']==mode and r['case_id']==case['id']])
                          for mode in MODES} for case in dataset['cases']},rows=rows)
    (out/'results.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (out/'fixture.json').write_bytes(FIXTURE.read_bytes())
    with (out/'metrics.csv').open('w',encoding='utf-8',newline='') as stream:
        names=['case_id','mode','trial','warmup','runtime_ok','contract_passed','model_calls','created_count',
               'memory_estimated_input_tokens','peak_estimated_input_tokens','storage_bytes_after','latency_ms','setup_ms']
        writer=csv.DictWriter(stream,fieldnames=names,extrasaction='ignore');writer.writeheader();writer.writerows(rows)
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',required=True,type=Path)
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--warmups',type=int,default=1)
    args=parser.parse_args()
    report=run_benchmark(args.output_dir,repeats=args.repeats,warmups=args.warmups)
    bad=[dict(case=r['case_id'],mode=r['mode'],errors=r['contract_errors']) for r in report['rows'] if not r['contract_passed']]
    print(json.dumps(dict(summaries=report['summaries'],failed=bad),ensure_ascii=False,indent=2))
    return int(bool(bad))


if __name__=='__main__':
    raise SystemExit(main())
