"""A07a fixed retrieval/build protocol. No answer evaluation or paid models.

python -m scripts.benchmarks.a07_evaluation --output-dir <new-directory>
Historical archives are optional explicit inputs, never CI dependencies. They
remain historical: missing A00 implementation/snippets are not reconstructed.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime,timezone
import csv
import gc
import hashlib
import json
from pathlib import Path
import socket
import statistics
import time
import tracemalloc
from types import SimpleNamespace
from unittest.mock import patch

from scripts.benchmarks import a00_baseline as a00, a01a_chunk_scale as scale, a02_retrieval as a02
from pycode.chunks import SnapshotBinding
from pycode.engine import PyCodeEngine


ROOT=Path(__file__).resolve().parents[2]
PROTOCOL=ROOT/'tests/fixtures/a07/experiment.json'
PROTOCOL_SHA256='9b22043b73009b84536908025ae113e18e446d4ed78d2a0079340489c3d61755'


def sha(data):return hashlib.sha256(data).hexdigest()


def read_protocol(path=PROTOCOL):
    protocol=json.loads(Path(path).read_text(encoding='utf-8'))
    canonical=sha(json.dumps(protocol,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode())
    if canonical!=PROTOCOL_SHA256:raise ValueError('A07 fixed protocol changed; review/version explicitly')
    data=(ROOT/protocol['question_fixture']).read_bytes()
    dataset=json.loads(data)
    if (sha(data)!=protocol['question_sha256'] or dataset['commit']!=protocol['corpus_commit']
            or len(dataset['cases'])!=protocol['question_count']
            or sum(c['truth_status']=='source_verified' for c in dataset['cases'])!=protocol['positive_count']):
        raise ValueError('Original A00 dataset identity mismatch')
    return protocol,dataset


def grouped(rows,dataset,ks):
    categories={c['id']:c['category'] for c in dataset['cases']}
    return {category:a02.aggregate([r for r in rows if categories[r['id']]==category],ks)
            for category in sorted(set(categories.values()))}


def a00_archive_metrics(value,dataset):
    """Independent path ranks; archived line coverage is ONLY re-aggregated."""
    cases={c['id']:c for c in dataset['cases']}
    if len(value['rows'])!=len(cases) or {r['id'] for r in value['rows']}!=set(cases):
        raise ValueError('Historical question identities differ')
    ks=sorted(map(int,value['summary']))
    rows=[]
    for row in value['rows']:
        case=cases[row['id']];scores={}
        for k in ks:
            ranks=a00.score([SimpleNamespace(path=p,snippet='') for p in row['retrieved_paths']],case['evidence'],k)
            cached=row['metrics'][str(k)]['snippet_line_coverage']
            scores[str(k)]=dict(recall=ranks['recall'],reciprocal_rank=ranks['reciprocal_rank'],
                archived_line_coverage=cached if case['truth_status']=='source_verified' else None)
        rows.append(dict(id=row['id'],category=case['category'],truth_status=case['truth_status'],metrics=scores))
    def aggregate(selected):
        return {str(k):{key:statistics.mean(r['metrics'][str(k)][key] for r in selected
                if r['metrics'][str(k)][key] is not None)
                if any(r['metrics'][str(k)][key] is not None for r in selected) else None
                for key in ('recall','reciprocal_rank','archived_line_coverage')} for k in ks}
    total=aggregate(rows)
    for k in ks:
        for key in ('recall','reciprocal_rank','archived_line_coverage'):
            expected=value['summary'][str(k)]['snippet_line_coverage' if key=='archived_line_coverage' else key]
            if abs(total[str(k)][key]-expected)>1e-12:raise ValueError('Historical aggregate cannot be verified')
    return dict(summary=total,categories={c:aggregate([r for r in rows if r['category']==c])
        for c in sorted({r['category'] for r in rows})},rows=rows,
        line_coverage_status='cached_case_scores_reaggregated_not_independently_recomputed_no_snippets',
        ndcg_status='not_recorded_in_original_A00')


def inspect_archive(path,label,protocol,dataset):
    data=Path(path).read_bytes()
    if sha(data)!=protocol['historical_receipts'][label]:raise ValueError('Historical archive receipt mismatch')
    value=json.loads(data)
    record=dict(classification='historical_archive',label=label,sha256=sha(data),reexecuted=False,
                current_latency_comparable=False)
    if label=='a01_scale':
        record.update(classification='historical_build_measurement',medians=value['medians'],
            scope=value['input_scope'],source_inventory_sha256=value['source_inventory_sha256'],
            same_corpus_as_current=False)
        return record
    if value['commit']!=dataset['commit'] or value['dataset_sha256']!=protocol['question_sha256']:
        raise ValueError('Historical corpus/question identity mismatch')
    record['corpus_sha256']=value['corpus_sha256']
    if label=='a00':
        record.update(classification='historical_A00_stub_archive',implementation_reproduced=False,
            exact_retriever_source_available=False,
            recorded_retriever_sha256=value['implementation_files']['pycode/retriever.py'],
            identity_limitation='A00 recorded Retriever has no matching local version; never substitute the corpus Commit',
            recomputed=a00_archive_metrics(value,dataset))
        if record['recorded_retriever_sha256']!=protocol['legacy_retriever_sha256']:
            raise ValueError('Historical Retriever fingerprint differs')
    else:
        if value['environment']['provider']!='fake':raise ValueError('Expected explicit historical Fake archive')
        ids=[r['id'] for r in value['rows']]
        if len(ids)!=len(dataset['cases']) or set(ids)!={c['id'] for c in dataset['cases']}:
            raise ValueError('Historical question identities differ')
        record.update(classification='historical_fake_engineering',summary=a02.aggregate(value['rows'],value['configuration']['ks']),
            categories=grouped(value['rows'],dataset,value['configuration']['ks']),
            graph_schema_version=value['configuration'].get('graph_schema_version'),
            configuration=value['configuration'])
    return record


def implementation():
    names=a00.git(ROOT,'ls-files','-z','--cached','--others','--exclude-standard','--',
                  'pycode','backend','scripts/benchmarks','tests/fixtures','tests/test_a07_evaluation.py').decode().split('\0')
    return dict(head=a00.git(ROOT,'rev-parse','HEAD').decode().strip(),
        files={n:sha((ROOT/n).read_bytes()) for n in sorted(set(names)-{''}) if (ROOT/n).is_file()})


def verify_implementation(before):
    if implementation()!=before:raise ValueError('Implementation/inputs changed during evaluation')


def measure_fixed_build(source,binding,output,rounds,batch_size):
    """Same Engine.index/allocator method as A01; pinned corpus needs no fake Git repo."""
    from pycode.chunks import load_chunk_index
    from pycode.semantic_index import SemanticIdentity
    from backend.app.infrastructure.artifacts import SnapshotArtifactStore
    from backend.app.integrations.semantic_index import SemanticIndexBuilder
    if tracemalloc.is_tracing():raise ValueError('Build measurement requires its own allocator session')
    samples=[]
    for number in range(rounds):
        root=output/str(number);root.mkdir(parents=True,exist_ok=False)
        paths=[root/(n+'.json') for n in ('index','graph','chunks')]
        gc.collect();tracemalloc.start()
        try:
            started=time.perf_counter()
            PyCodeEngine().index(source,paths[0],graph_output_path=paths[1],chunk_output_path=paths[2],binding=binding)
            elapsed=(time.perf_counter()-started)*1000
            _,peak=tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
        samples.append(dict(build_ms=elapsed,python_peak_bytes=peak,
            artifact_bytes={p.stem:p.stat().st_size for p in paths},artifact_sha256={p.stem:sha(p.read_bytes()) for p in paths}))
    if any(s['artifact_sha256']!=samples[0]['artifact_sha256'] for s in samples):
        raise ValueError('Repeated full builds are not deterministic')
    chunks=load_chunk_index(paths[2],binding)
    class CountedFake(a02.HashFakeProvider):
        calls=0
        def embed_documents(self,texts):
            self.calls+=1
            return super().embed_documents(texts)
    provider=CountedFake()
    store_root=output/'semantic';(store_root/binding.project_id).mkdir(parents=True)
    store=SnapshotArtifactStore(store_root);builder=SemanticIndexBuilder(store)
    target=SemanticIdentity(binding,sha(paths[2].read_bytes()),provider.provider,provider.model,provider.dimension)
    started=time.perf_counter();first=builder.build(target,chunks,provider,batch_size=batch_size)
    initial_ms=(time.perf_counter()-started)*1000;calls=provider.calls
    ready=store.semantic_directory(target)/'ready'
    hashes={p.name:sha(p.read_bytes()) for p in ready.iterdir() if p.is_file()}
    started=time.perf_counter();second=builder.build(target,chunks,provider,batch_size=batch_size)
    reuse_ms=(time.perf_counter()-started)*1000
    if (first!=second or provider.calls!=calls
            or hashes!={p.name:sha(p.read_bytes()) for p in ready.iterdir() if p.is_file()}):
        raise ValueError('Published Fake artifact was rebuilt or changed during reuse')
    return dict(classification='current_implementation_fake_engineering',binding=asdict(binding),rounds=samples,
        median_build_ms=statistics.median(s['build_ms'] for s in samples),
        timing='full Engine.index with tracemalloc; capture/readback excluded; no warmup; not RSS',
        chunks=len(chunks.chunks),fake_embedding=dict(initial_build_ms=initial_ms,reuse_ms=reuse_ms,
            document_calls=calls,repeat_document_calls=provider.calls-calls,artifact_files=hashes,
            artifact_bytes=sum(p.stat().st_size for p in ready.iterdir() if p.is_file()),segments=first['vector_count']),
        incremental_indexing='not_implemented',repeated_full_builds_identical=True)


def run_evaluation(output,*,profile='acceptance',protocol_path=PROTOCOL,archives=None):
    protocol,dataset=read_protocol(protocol_path)
    if profile not in protocol['profiles']:raise ValueError('Unknown fixed evaluation profile')
    conditions=protocol['profiles'][profile]
    output=Path(output).resolve()
    if output.exists():raise FileExistsError('Refuse to overwrite an evaluation')
    before=implementation()
    corpus=a00.read_corpus(ROOT,dataset['commit']);a00.validate_dataset(dataset,corpus)
    corpus_hash=sha(json.dumps({n:sha(b) for n,b in corpus.items()},sort_keys=True).encode())
    historical={label:inspect_archive(path,label,protocol,dataset) for label,path in (archives or {}).items()}
    if any(v.get('corpus_sha256',corpus_hash)!=corpus_hash for v in historical.values()):
        raise ValueError('Historical corpus hash differs')
    output.mkdir(parents=True,exist_ok=False)
    (output/'protocol.json').write_bytes(Path(protocol_path).read_bytes())
    if archives:
        (output/'historical-inputs').mkdir()
        for label,path in archives.items():(output/'historical-inputs'/(label+'.json')).write_bytes(Path(path).read_bytes())
    started=datetime.now(timezone.utc).isoformat();retrievals={}
    with patch.object(socket.socket,'connect',a02._offline),patch.object(socket,'getaddrinfo',a02._offline):
        for budget in conditions['budgets']:
            destination=output/f'retrieval-{budget}'
            result=a02.run_benchmark(ROOT,ROOT/protocol['question_fixture'],destination,
                repeats=conditions['repeats'],warmup=conditions['warmup'],candidate_limit=protocol['candidate_limit'],
                top_k=protocol['top_k'],budget=budget,structured_graph=protocol['structured_graph'])
            if (result['configuration']['graph_schema_version']!=protocol['graph_schema_version']
                    or any(result['configuration'][k]!=v for k,v in protocol['graph'].items())):
                raise ValueError('Expected current structural Graph capability')
            retrievals[str(budget)]=dict(classification='current_implementation_fake_engineering',
                result_path=f'retrieval-{budget}/results.json',summary=result['summary'],
                categories=grouped(result['rows'],dataset,result['configuration']['ks']),
                configuration=result['configuration'],setup=result['setup'],binding=result['binding'])
        fixed_build=measure_fixed_build(destination/'corpus',SnapshotBinding(**result['binding']),
            output/'fixed-build',conditions['build_rounds'],protocol['embedding']['lifecycle_build_batch_size'])
        if conditions['repository_scale']:
            current_scale=scale.measure_repository(ROOT,rounds=conditions['build_rounds'])
            current_scale['classification']='current_implementation_source_scale_not_retrieval_quality'
            (output/'repository-scale.json').write_text(json.dumps(current_scale,indent=2)+'\n',encoding='utf-8')
            scale_summary={k:current_scale[k] for k in ('classification','source_inventory_sha256','medians','binding','config')}
        else:scale_summary=dict(classification='not_measured_test_profile')
    verify_implementation(before)
    final=dict(schema_version=1,kind=protocol['kind'],profile=profile,conditions=conditions,
        started_at_utc=started,finished_at_utc=datetime.now(timezone.utc).isoformat(),implementation=before,
        protocol_sha256=PROTOCOL_SHA256,question_sha256=protocol['question_sha256'],corpus_commit=dataset['commit'],
        corpus_sha256=corpus_hash,corpus_files={n:sha(b) for n,b in corpus.items()},question_count=len(dataset['cases']),
        truth_status=dict(Counter(c['truth_status'] for c in dataset['cases'])),
        prompt_versions={n:sha((ROOT/n).read_bytes()) for n in
            ('pycode/prompt_builder.py','pycode/agent/prompt_sections.py','pycode/agent/llm_planner.py','pycode/agent/context_budget.py')},
        embedding=protocol['embedding'],llm=protocol['llm'],historical=historical,
        legacy_A00_reproduction=dict(status='not_reproduced',source_fingerprint_gap=True,
            recorded_retriever_sha256=protocol['legacy_retriever_sha256']),
        retrievals=retrievals,fixed_corpus_build=fixed_build,repository_scale=scale_summary,
        implementation_unchanged=True,llm_calls=0,real_model_calls=0,real_model_results=None,answer_quality=None,
        paid_requests=0,real_model_cost=None)
    (output/'results.json').write_text(json.dumps(final,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    with (output/'retrieval-metrics.csv').open('w',encoding='utf-8',newline='') as stream:
        names=['classification','budget','category','strategy','comparison_scope','k',
               'file_recall','file_mrr','file_ndcg','line_coverage','symbol_recall','duplicate_line_rate','duplicate_file_rate']
        writer=csv.DictWriter(stream,fieldnames=names,extrasaction='ignore');writer.writeheader()
        for budget,result in retrievals.items():
            for category,summary in {'all':result['summary'],**result['categories']}.items():
                for strategy,values in summary.items():
                    for k,metrics in values['selected'].items():
                        writer.writerow(dict(classification=result['classification'],budget=budget,category=category,
                            strategy=strategy,comparison_scope='production_intent_replay' if strategy=='production_hybrid'
                            else 'four_route_general_ablation',k=k,**metrics))
    return final


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir',required=True,type=Path)
    parser.add_argument('--profile',choices=['acceptance','test'],default='acceptance')
    parser.add_argument('--a00-history',type=Path)
    parser.add_argument('--a02-budget12000-history',type=Path)
    parser.add_argument('--a02-budget131072-history',type=Path)
    parser.add_argument('--a01-scale-history',type=Path)
    args=parser.parse_args()
    archives={k:v for k,v in {'a00':args.a00_history,'a02_budget12000':args.a02_budget12000_history,
        'a02_budget131072':args.a02_budget131072_history,'a01_scale':args.a01_scale_history}.items() if v is not None}
    result=run_evaluation(args.output_dir,profile=args.profile,archives=archives)
    print(json.dumps(dict(output=str(args.output_dir),profile=result['profile'],questions=result['question_count'],
        real_model_results=result['real_model_results'],legacy_reproduction=result['legacy_A00_reproduction']),indent=2))


if __name__=='__main__':main()
