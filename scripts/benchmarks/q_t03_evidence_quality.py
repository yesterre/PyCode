"""Q-T03 source/evidence evaluation. No model, paid embedding, DB or Redis.

python -m scripts.benchmarks.q_t03_evidence_quality --implementation PATH --output-dir NEW
python -m scripts.benchmarks.q_t03_evidence_quality --summarize results.json
python -m scripts.benchmarks.q_t03_evidence_quality --compare BEFORE AFTER --output FILE

Quality observers see source labels only AFTER retrieval and prompt rendering.
Actual model delivery, generated citation semantics and answer quality are N/A.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
import gc
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import platform
import re
import socket
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5


ROOT=Path(__file__).resolve().parents[2]
FIXTURE=ROOT/'tests/fixtures/q_t03'


def sha(data):return hashlib.sha256(data).hexdigest()
def read(path):return json.loads(Path(path).read_text(encoding='utf-8'))
def write(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
def canonical(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()
def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT)
def ratio(n,d):return dict(numerator=n,denominator=d,value=n/d if d else None)
def quantiles(values):
    return dict(count=len(values),median=statistics.median(values) if values else None,
                p95=sorted(values)[math.ceil(.95*len(values))-1] if values else None)


def locked_inputs():
    lock=read(FIXTURE/'lock.json')
    for path,expected in lock['files'].items():
        if sha((ROOT/path).read_bytes())!=expected:raise ValueError('Frozen Q-T03 input changed: '+path)
    return read(FIXTURE/'protocol.json'),read(FIXTURE/'questions.json')['cases'],lock


def corpus_sources(protocol):
    values={}
    original=protocol['corpora']['a00']
    paths=git('ls-tree','-r','--name-only',original['commit'],*original['prefixes']).decode().splitlines()
    values['a00']={p:git('show',original['commit']+':'+p).decode('utf-8-sig') for p in paths if p.endswith('.py')}
    real=protocol['corpora']['itsdangerous'];provenance=read(ROOT/real['provenance'])
    if provenance['commit']!=real['commit']:raise ValueError('Upstream Commit mismatch')
    for path,expected in provenance['sha256'].items():
        if sha((ROOT/real['provenance']).parent.joinpath(path).read_bytes())!=expected:raise ValueError('Upstream bytes changed')
    base=ROOT/real['fixture']
    values['itsdangerous']={p.relative_to(base).as_posix():p.read_text(encoding='utf-8') for p in sorted(base.rglob('*.py'))}
    values['synthetic']=read(ROOT/protocol['corpora']['synthetic']['fixture'])
    values['legacy']=values['synthetic']
    return values


def offline_vectors(chunks,binding):
    """A02-style signed token hashing; a plumbing substitute, not semantics."""
    from pycode.semantic_search import VectorHit,VectorResult
    def vector(text):
        row=[0.]*64;row[0]=1.
        for token in re.findall(r'[a-z_][a-z_0-9]*|[\u4e00-\u9fff]',text.lower()):
            h=hashlib.sha256(token.encode()).digest()
            row[1+int.from_bytes(h[:4],'little')%63]+=1 if h[4]&1 else -1
        norm=math.hypot(*row)
        return tuple(v/norm for v in row)
    rows=[(c,vector(c.content)) for c in chunks.chunks]
    def query(text,limit):
        q=vector(text);scores={};spans={}
        for c,v in rows:
            key=(c.path,c.span.symbol_key,c.span.symbol_start_line,c.span.symbol_end_line)
            score=max(-1.,min(1.,math.fsum(a*b for a,b in zip(q,v))))
            scores[key]=max(scores.get(key,-math.inf),score)
            if key not in spans or (c.span.start_line,c.span.part_index)<(spans[key].start_line,spans[key].part_index):spans[key]=c.span
        order=sorted(scores,key=lambda k:(-scores[k],k[0],k[2],k[3],k[1]))[:limit]
        return VectorResult(binding,tuple(VectorHit(k[0],spans[k],scores[k]) for k in order))
    return query


def metadata_blocks(prompt):
    # Keep every rendered metadata byte; source snippets are saved separately.
    return re.findall(r'## 证据 \d+:.*?\n代码片段:\n',prompt,re.S)


def disclosure(items,blocks):
    """Provenance delivery, NOT graph soundness or language entailment."""
    lines='\n'.join(blocks).splitlines();records=[]
    for index,item in enumerate(items):
        for step in item.get('graph_path',[]):
            status=step.get('resolution') or 'unavailable'
            if status not in {'static','declaration','unknown','ambiguous','legacy'}:status='unavailable'
            basis=step.get('basis') or 'unavailable'
            related=[line for line in lines if step['source'] in line and step['target'] in line
                     and '--'+step['relation']+'-->' in line]
            present=any('resolution='+status in line and 'basis='+basis in line and 'runtime=unverified' in line for line in related)
            conflicting=any(re.search(r'resolution=(\w+)',line) and
                re.search(r'resolution=(\w+)',line).group(1)!=status for line in related)
            records.append(dict(item=index,step=step,expected_resolution=status,expected_basis=basis,
                                disclosed=present,conflicting=conflicting))
    return records


def line_set(items):
    return {(i['path'],n) for i in items if i.get('path') and type(i.get('start_line')) is int
            for n in range(i['start_line'],i['end_line']+1)}


def expected_lines(evidence):
    return {(e['path'],n) for e in evidence for s,e_line in e['minimal_ranges'] for n in range(s,e_line+1)}


def covered(e,items):return expected_lines([e])<=line_set(items)


def score(row,truth):
    gold=truth['evidence'];selected=row['selected'];candidate=row['candidates'];fast=row['fast_visible'];agent=row['agent_visible']
    needed=expected_lines(gold);files={e['path'] for e in gold}
    symbols={(e['path'],e['symbol']) for e in gold if e['symbol'] is not None}
    def metrics(items):
        items=[i for i in items if i.get('path')]
        paths={i['path'] for i in items};names={(i['path'],i.get('symbol')) for i in items};lines=line_set(items)
        return dict(evidence_file=ratio(sum(e['path'] in paths for e in gold),len(gold)),
            evidence_symbol=ratio(sum((e['path'],e['symbol']) in names for e in gold if e['symbol'] is not None),len(symbols)),
            evidence_lines=ratio(sum(covered(e,items) for e in gold),len(gold)),
            file_recall=ratio(len(paths&files),len(files)),symbol_recall=ratio(len(symbols&names),len(symbols)),
            line_coverage=ratio(len(needed&lines),len(needed)))
    relevant=[any(covered(e,[i]) for e in gold) for i in selected]
    credited=set();gains=[]
    for item in selected:
        matches=[j for j,e in enumerate(gold) if j not in credited and covered(e,[item])]
        gains.append(bool(matches))
        if matches:credited.add(matches[0])
    first=next((i for i,v in enumerate(relevant,1) if v),None)
    ideal=sum(1/math.log2(i+1) for i in range(1,min(truth['top_k'],len(gold))+1))
    counts=Counter((i['path'],n) for i in selected if type(i.get('start_line')) is int for n in range(i['start_line'],i['end_line']+1))
    total=sum(counts.values())
    return dict(candidate=metrics(candidate),selected=metrics(selected),fast_rendered=metrics(fast),agent_rendered=metrics(agent),
        necessary_unit_precision=ratio(sum(relevant),len(relevant)) if gold else ratio(0,0),
        evidence_mrr=1/first if first else 0. if gold else None,
        evidence_ndcg=sum(int(g)/math.log2(i+1) for i,g in enumerate(gains,1))/ideal if ideal else None,
        duplicate_lines=ratio(total-len(counts),total))


def aggregate(rows):
    summary=dict(questions=len(rows),real_llm_calls=0,paid_embeddings=0)
    for stage in ('candidate','selected','fast_rendered','agent_rendered'):
        summary[stage]={name:ratio(sum(r['metrics'][stage][name]['numerator'] for r in rows),
                                  sum(r['metrics'][stage][name]['denominator'] for r in rows))
                        for name in ('evidence_file','evidence_symbol','evidence_lines','file_recall','symbol_recall','line_coverage')}
    for metric in ('necessary_unit_precision','duplicate_lines'):
        summary[metric]=ratio(sum(r['metrics'][metric]['numerator'] for r in rows),sum(r['metrics'][metric]['denominator'] for r in rows))
    for metric in ('evidence_mrr','evidence_ndcg'):
        v=[r['metrics'][metric] for r in rows if r['metrics'][metric] is not None]
        summary[metric]=dict(sum=sum(v),denominator=len(v),value=sum(v)/len(v) if v else None)
    refs=[ref for r in rows for ref in r['reference_checks']]
    summary['evidence_address_validity']=ratio(sum(r['address_valid'] for r in refs),len(refs))
    summary['evidence_source_consistency']=ratio(sum(r['source_consistent'] for r in refs),len(refs))
    widths=[r['end_line']-r['start_line']+1 for r in refs if r['address_valid']]
    summary['reference_span_width']=dict(**quantiles(widths),over_40=ratio(sum(w>40 for w in widths),len(widths)))
    proofs=[v for r in rows for v in r['relation_disclosure']]
    summary['fast_relation_provenance_disclosure']=ratio(sum(v['disclosed'] for v in proofs),len(proofs))
    summary['conflicting_relation_statuses']=sum(v['conflicting'] for v in proofs)
    summary['relation_kinds']=dict(Counter(v['step']['relation'] for v in proofs))
    summary['relation_statuses']=dict(Counter(v['expected_resolution'] for v in proofs))
    relations=[v for r in rows for v in r['source_relation_audit']]
    summary['stored_graph_conformance_to_source_oracle']=ratio(sum(v['conforms'] for v in relations),len(relations))
    summary['source_relation_statuses']=dict(Counter(v['classification'] for v in relations))
    for name in ('retrieval_seconds','pack_seconds'):
        summary[name]=quantiles([v for r in rows for v in r['timings'][name]])
    for name in ('fast_prompt_utf8_bytes','fast_evidence_utf8_bytes','pack_utf8_upper_bound','agent_prompt_utf8_bytes','selected_count'):
        summary[name]=quantiles([r['cost'][name] for r in rows])
    summary['not_evaluated']={key:'No real model answers or external semantic adjudication; source references/rendered inputs are measured separately.' for key in
        ('actual_model_evidence_delivery','answer_citation_address_validity','citation_semantic_support_precision',
         'claim_evidence_coverage','unsupported_claim_rate','factual_correctness','answer_completeness','claim_support_rate','billed_tokens','actual_cost')}
    return summary


def recompute(data):
    truth={c['id']:c for c in data['questions']};rows=[]
    for original in data['rows']:
        row=dict(original);row['metrics']=score(row,truth[row['id']]);rows.append(row)
    return dict(overall=aggregate(rows),
                by_category={k:aggregate([r for r in rows if r['category']==k]) for k in sorted({r['category'] for r in rows})},
                by_partition={k:aggregate([r for r in rows if r['split']==k]) for k in sorted({r['split'] for r in rows})})


def compare(before,after):
    for key in ('protocol','questions','input_lock','environment','source_hashes','graph_hashes'):
        if before[key]!=after[key]:raise ValueError('Non-comparable Q-T03 runs: '+key)
    b,a=recompute(before),recompute(after);changes=[]
    for x,y in zip(before['rows'],after['rows']):
        if x['id']!=y['id']:raise ValueError('Question order changed')
        changes.append(dict(id=x['id'],category=x['category'],split=x['split'],
            candidates_same=x['candidates']==y['candidates'],selected_same=x['selected']==y['selected'],
            graph_disclosure_before=x['relation_disclosure'],graph_disclosure_after=y['relation_disclosure'],
            metrics_before=score(x,{c['id']:c for c in before['questions']}[x['id']]),
            metrics_after=score(y,{c['id']:c for c in after['questions']}[y['id']]),
            cost_before=x['cost'],cost_after=y['cost']))
    return dict(before=b,after=a,per_question=changes,
                interpretation='Relation provenance delivery is a rendered-input metric, not runtime binding soundness or answer semantics.')


def run(args):
    protocol,questions,lock=locked_inputs()
    if args.output_dir.exists():raise FileExistsError('Use a new result directory; never overwrite a previous run')
    implementation=args.implementation.resolve();sys.path.insert(0,str(implementation))
    os.environ['PYTHON_DOTENV_DISABLED']='1';os.environ['PYCODE_EMBEDDING_ENABLED']='false'
    from pycode.chunks import SnapshotBinding,build_chunk_index
    from pycode.engine import PyCodeEngine
    from pycode.graph_builder import build_code_graph
    from pycode.graph_schema import graph_to_dict
    from pycode.retrieval_evidence import Query,RetrievalRequest,RetrievalContext,source_lines
    from pycode import retriever
    from pycode.evidence_pack import render_evidence,tool_items
    from pycode.prompt_builder import build_code_qa_prompt
    from pycode.tools import ToolContext,retrieve_context
    from pycode.agent.budget import ExecutionLimits,ExecutionBudget
    from pycode.agent.types import AgentTask
    from pycode.agent.prompts import build_agent_summary_context,render_agent_prompt
    from pycode.agent.evidence import inspect_context_evidence
    if not Path(retriever.__file__).resolve().is_relative_to(implementation):raise ValueError('Wrong implementation imported')
    sources=corpus_sources(protocol);engine=PyCodeEngine();contexts={};graphs={}
    source_hashes={name:{p:sha(s.encode()) for p,s in files.items()} for name,files in sources.items()}
    impl_files={p.relative_to(implementation).as_posix():sha(p.read_bytes()) for p in sorted((implementation/'pycode').rglob('*.py'))}
    data=dict(version=1,started_at=datetime.now(timezone.utc).isoformat(),protocol=protocol,questions=questions,input_lock=lock,
        environment=dict(python=sys.version,platform=platform.platform(),dependencies={name:importlib.metadata.version(name) for name in ('pytest','pydantic','httpx','openai')}),
        implementation=dict(base_commit=protocol['baseline_commit'],label=args.label,files=impl_files),source_hashes=source_hashes,graph_hashes=graphs,rows=[],
        limitations=['No real/fake answers; all generated-answer and citation-semantic quality metrics are not_evaluated.',
                    'Source annotation compiled by development agent; no external human adjudicator.',
                    'Offline vectors test retrieval engineering, not real embedding quality.',
                    'Rendered model inputs are not externally delivered model requests.'])
    temp_parent=ROOT/'.pytest_tmp_qt03/runs';temp_parent.mkdir(parents=True,exist_ok=True)
    assert temp_parent.resolve().is_relative_to(ROOT.resolve())
    def forbidden(*a,**k):raise AssertionError('Network/model calls forbidden in Q-T03')
    observed={};original_pack=retriever.build_pack
    def pack(result,budget,limit):
        start=perf_counter();value=original_pack(result,budget,limit);elapsed=perf_counter()-start
        observed.update(candidates=result.items,pack_seconds=elapsed)
        return value
    with TemporaryDirectory(prefix='run-',dir=temp_parent) as temporary,patch.object(socket.socket,'connect',forbidden),patch.object(retriever,'build_pack',pack),patch('pycode.engine.OpenAIResponsesClient',forbidden):
        for name,files in sources.items():
            root=Path(temporary)/name
            for path,source in files.items():
                dest=root/path;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(source,encoding='utf-8')
            idx=engine.build_index(root,include_chunk_metadata=True,include_graph_metadata=True)
            commit=protocol['corpora'][name].get('commit') or sha(canonical(files))[:40]
            binding=SnapshotBinding(str(uuid5(NAMESPACE_URL,'qt03-project-'+name)),str(uuid5(NAMESPACE_URL,'qt03-snapshot-'+name)),commit)
            chunks=build_chunk_index(idx,binding)
            graph_index=(replace(idx,structure_version=None,files=[replace(f,structure=None) for f in idx.files])
                         if name=='legacy' else idx)
            graph=build_code_graph(graph_index,binding=None if name=='legacy' else binding)
            graphs[name]=sha(canonical(graph_to_dict(replace(graph,project_path='<frozen-'+name+'>'))))
            ctx=RetrievalContext.create(root,idx,graph,binding=binding,chunks=chunks)
            contexts[name]=replace(ctx,**{k:v for k,v in protocol['retrieval'].items() if k in ctx.__dataclass_fields__},retrieval_mode='hybrid',
                                   vector_query=offline_vectors(chunks,binding)) if 'retrieval_mode' not in protocol['retrieval'] else ctx
        for truth in questions:
            # Labels do not cross this boundary into retrieval or prompt assembly.
            public={k:truth[k] for k in ('id','corpus','category','question','split','intent','target','top_k')}
            ctx=contexts[public['corpus']];request=RetrievalRequest(Query(public['question'],public['intent'],public['target']),public['top_k'])
            for _ in range(protocol['warmup']):retriever.retrieve(request,ctx)
            start=perf_counter();result=retriever.retrieve(request,ctx);elapsed=perf_counter()-start
            candidates=[dict(i.reference(),symbol_key=i.symbol_key) for i in observed['candidates']]
            selected=[i.to_dict() for i in result.items];fast=build_code_qa_prompt(result);blocks=metadata_blocks(fast)
            fast_visible=[dict(i.reference(),symbol_key=i.symbol_key) for i in result.items if i.snippet and i.snippet in fast]
            tool=retrieve_context(ToolContext(ctx.root,retrieval_context=ctx),public['question'],intent=public['intent'],target=public['target'])
            assert tool.ok and tool.data['items']==tool_items(result.items)
            issues,rejected=inspect_context_evidence('retrieve_context',tool.data,ctx)
            assert not rejected
            budget=ExecutionBudget(ExecutionLimits(**protocol['agent']),binding=ctx.binding);budget.retrieval_context=ctx
            task=AgentTask(public['question'],ctx.root)
            with budget.activate():
                assembled=build_agent_summary_context(task,[],[tool],tools={},tasks=[],load_tasks=False)
                agent=render_agent_prompt(assembled)
            assert not assembled.report['delivered_to_model']
            checks=[]
            for item in selected:
                lines=source_lines(dict(ctx.sources)[item['path']]);s,e=item['start_line'],item['end_line']
                valid=(item['project_id'],item['snapshot_id'],item['commit_sha'])==(ctx.binding.project_id,ctx.binding.snapshot_id,ctx.binding.commit_sha) and type(s) is int and type(e) is int and 1<=s<=e<=len(lines)
                expected='\n'.join(f'{n}: {lines[n-1]}' for n in range(s,e+1)) if valid else None
                checks.append(dict(path=item['path'],symbol=item['symbol'],start_line=s,end_line=e,address_valid=valid,source_consistent=valid and item['snippet']==expected))
            relation_audit=[]
            nodes={n.id:n for n in ctx.graph.nodes}
            for label in truth['relations']:
                matches=[s for s in ctx.graph.call_sites if s['path']==label['path'] and s['start_line']==label['line'] and s['col_offset']==label['column'] and s['expression']==label['expression'] and nodes[s['caller_id']].name==label['scope']]
                actual=matches[0] if len(matches)==1 else None
                conforms=(actual is not None and (actual['resolution'] in {'unknown','ambiguous'} if label['expected']=='unknown_runtime' else actual['resolution']=='static' and actual['target_id']==label['target_id']))
                classification=('unknown_ambiguous' if actual and actual['resolution'] in {'unknown','ambiguous'} else 'static_confirmed' if conforms else 'overasserted_or_unavailable')
                relation_audit.append(dict(label=label,actual=actual,conforms=conforms,classification=classification))
            row=dict(**public,candidates=candidates,selected=selected,pack=result.pack.summary(),status=result.status,diagnostics=result.diagnostics,
                fast_metadata_blocks=blocks,fast_prompt_sha256=sha(fast.encode()),fast_visible=fast_visible,agent_prompt_sha256=sha(agent.encode()),
                agent_visible=budget.answer_references,agent_report=assembled.report,agent_warnings=issues,reference_checks=checks,
                relation_disclosure=disclosure(selected,blocks),source_relation_audit=relation_audit,
                actual_model_delivery='not_evaluated',generated_answer=None,
                cost=dict(fast_prompt_utf8_bytes=len(fast.encode()),fast_evidence_utf8_bytes=len(render_evidence(result.items).encode()),
                    pack_utf8_upper_bound=result.pack.estimated_tokens,pack_budget=result.pack.budget,agent_prompt_utf8_bytes=len(agent.encode()),selected_count=len(selected),
                    real_llm_calls=0,paid_embedding_calls=0,actual_tokens=None,billed_tokens=None),
                timings=dict(retrieval_seconds=[],pack_seconds=[]))
            for _ in range(protocol['repeats']):
                gc.collect();start=perf_counter();retriever.retrieve(request,ctx)
                row['timings']['retrieval_seconds'].append(perf_counter()-start);row['timings']['pack_seconds'].append(observed['pack_seconds'])
            row['metrics']=score(row,truth);data['rows'].append(row)
    data['summaries']=recompute(data)
    args.output_dir.mkdir(parents=True)
    write(args.output_dir/'results.json',data)
    write(args.output_dir/'receipt.json',dict(results_sha256=sha((args.output_dir/'results.json').read_bytes()),input_lock=lock,
        implementation=data['implementation'],graph_hashes=graphs,created_before_after_comparison=True))
    return data['summaries']['by_partition']['development']


def export_baseline(destination):
    protocol,_,_=locked_inputs();commit=protocol['baseline_commit']
    if len(commit)!=40 or any(c not in '0123456789abcdef' for c in commit):raise ValueError('Full Commit required')
    if git('rev-parse','--verify',commit+'^{commit}').decode().strip()!=commit:raise ValueError('Commit mismatch')
    if destination.exists():raise FileExistsError('Export destination must be new')
    for name in git('ls-tree','-r','--name-only',commit,'pycode').decode().splitlines():
        if name.endswith('.py'):
            path=destination/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(git('show',commit+':'+name))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--implementation',type=Path,default=ROOT);p.add_argument('--output-dir',type=Path)
    p.add_argument('--label',default='working-tree');p.add_argument('--summarize',type=Path)
    p.add_argument('--compare',nargs=2,type=Path);p.add_argument('--output',type=Path)
    p.add_argument('--export-baseline',type=Path)
    args=p.parse_args()
    if args.export_baseline:
        export_baseline(args.export_baseline)
        return
    if args.summarize:value=recompute(read(args.summarize))
    elif args.compare:value=compare(*(read(path) for path in args.compare))
    else:
        if args.output_dir is None:p.error('Use a new --output-dir')
        value=run(args)
    if args.output:write(args.output,value)
    print(json.dumps(value if not args.compare else dict(questions=len(value['per_question']),selection_changes=sum(not r['selected_same'] for r in value['per_question'])),ensure_ascii=False,indent=2))


if __name__=='__main__':main()
