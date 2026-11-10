"""Offline F-T05-01 evaluation; fixture source is parsed, never imported/executed.

Run with the same interpreter against a Git-HEAD source export and the workspace:
  python -m scripts.benchmarks.f_t05_graph_resolution --implementation PATH --output FILE
  python -m scripts.benchmarks.f_t05_graph_resolution --summarize FILE
  python -m scripts.benchmarks.f_t05_graph_resolution --compare BEFORE AFTER --output FILE
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
import gc
import hashlib
import json
import math
from pathlib import Path
import platform
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import perf_counter
import tracemalloc


ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'tests/fixtures/f_t05'
BASELINE_COMMIT = '266223c833514af7d414bbf31e527b4b994ce786'
BUILD_PARAMETERS = {'include_graph_metadata': True, 'include_chunk_metadata': True}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def site_key(site):
    return tuple(site[k] for k in ('path', 'scope', 'line', 'column', 'expression'))


def distribution(sites):
    return {key: dict(sorted(Counter(s[key] for s in sites).items()))
            for key in ('resolution', 'basis')}


def summarize(sites, all_sites):
    definite = [s for s in sites if s['actual']['resolution'] == 'static']
    correct = sum(s['expected'] == 'static' and s['actual']['target_id'] == s['target_id'] for s in definite)
    unique = sum(s['expected'] == 'static' for s in sites)
    wrong = len(definite) - correct
    statuses = Counter(s['actual']['resolution'] for s in sites)
    return dict(labelled=len(sites), unique_targets=unique, must_stay_uncertain=len(sites)-unique,
                correct_bindings=correct, definite_bindings=len(definite), wrong_bindings=wrong,
                precision=correct/len(definite) if definite else None,
                recall=correct/unique if unique else None,
                misbinding_rate=wrong/len(sites) if sites else None,
                unknown=statuses['unknown'], ambiguous=statuses['ambiguous'],
                unknown_ratio=statuses['unknown']/len(sites) if sites else None,
                ambiguous_ratio=statuses['ambiguous']/len(sites) if sites else None,
                labelled_distribution=distribution([s['actual'] for s in sites]),
                all_calls=len(all_sites), unlabelled=len(all_sites)-len(sites),
                all_call_distribution=distribution(all_sites))


def recompute(result):
    return {name: summarize(data['labelled_sites'], data['all_sites'])
            for name, data in result['corpora'].items()}


def fixtures():
    synthetic = read_json(FIXTURES/'cases.json')['cases']
    existing = ROOT/'tests/fixtures/a03/receivers'
    labels = [
        ('consumer.py','<module>/Consumer[1]/run[1]',9,8,'self.service.work','method:service.py:Service.work'),
        ('consumer.py','<module>/Consumer[1]/run[1]',10,8,'self.helper','method:consumer.py:Consumer.helper'),
        ('consumer.py','<module>/Consumer[1]/kind[1]',17,15,'cls.code','method:consumer.py:Consumer.code'),
        ('main.py','<module>/main[1]',10,4,'saved.run','method:consumer.py:Consumer.run'),
        ('main.py','<module>/main[1]',11,4,'Service.label','method:service.py:Service.label'),
        ('main.py','<module>/main[1]',12,4,'ImportedConsumer.kind','method:consumer.py:Consumer.kind'),
    ]
    synthetic = synthetic + [dict(id='existing-receivers', category='existing_fixture',
        files={p.relative_to(existing).as_posix(): p.read_text(encoding='utf-8') for p in sorted(existing.glob('*.py'))},
        labels=[dict(zip(('path','scope','line','column','expression','target_id'), row), expected='static',
                     rationale='Existing closed nominal receiver fixture, independently specified method identity.') for row in labels])]
    provenance = read_json(FIXTURES/'itsdangerous/PROVENANCE.json')
    for path, expected in provenance['sha256'].items():
        if digest((FIXTURES/'itsdangerous'/path).read_bytes()) != expected:
            raise ValueError('Pinned upstream source hash mismatch: ' + path)
    real = dict(id='itsdangerous', category='real_repository',
                files={p.relative_to(FIXTURES/'itsdangerous/src').as_posix(): p.read_text(encoding='utf-8')
                       for p in sorted((FIXTURES/'itsdangerous/src').rglob('*.py'))},
                labels=read_json(FIXTURES/'real_labels.json'))
    return dict(synthetic=synthetic, itsdangerous=[real])


def percentiles(values):
    # Nearest-rank P95, intentionally not an interpolated stable tail estimate.
    return dict(samples=values, median=statistics.median(values),
                p95=sorted(values)[math.ceil(.95*len(values))-1])


def evaluate(args):
    implementation = args.implementation.resolve()
    sys.path.insert(0, str(implementation))
    from pycode.chunks import SnapshotBinding, build_chunk_index
    from pycode.engine import PyCodeEngine
    from pycode.graph_builder import build_code_graph
    from pycode.graph_schema import graph_from_dict, graph_to_dict, validate_graph
    from pycode.retrieval_evidence import RetrievalContext
    from pycode.tools import ToolContext, query_code_graph
    import pycode.symbol_resolver as resolver
    if not Path(resolver.__file__).resolve().is_relative_to(implementation):
        raise ValueError('Implementation import escaped the selected source tree')
    dataset = fixtures()
    frozen = json.dumps(dataset, sort_keys=True, ensure_ascii=False).encode()
    py_files = {p.relative_to(implementation).as_posix(): digest(p.read_bytes())
                for p in sorted((implementation/'pycode').rglob('*.py'))}
    result = dict(format_version=1, baseline_commit=BASELINE_COMMIT,
                  implementation=dict(source_sha256=py_files,
                      aggregate_sha256=digest(json.dumps(py_files, sort_keys=True).encode()),
                      base_commit=args.base_commit, label=args.label),
                  environment=dict(python=sys.version, platform=platform.platform(), machine=platform.machine()),
                  generated_at=datetime.now(timezone.utc).isoformat(),
                  dataset_sha256=digest(frozen), build_parameters=BUILD_PARAMETERS,
                  provenance=read_json(FIXTURES/'itsdangerous/PROVENANCE.json'),
                  methodology=dict(warmup=1, timing_repetitions=args.repeats, memory_repetitions=3,
                      timer='time.perf_counter; GC before each pass, excluded from timer',
                      index='disk scan + read + AST parse + chunk spans + structural extraction',
                      graph='GraphBuilder, receiver resolution, edges, full Schema validation (including replay)',
                      memory='separate tracemalloc passes; Python tracked allocations, not process RSS',
                      p95='nearest rank; small sample, not a stable tail latency estimate'),
                  corpora={}, queries=[])
    engine = PyCodeEngine()
    binding = SnapshotBinding('00000000-0000-0000-0000-000000000001',
                              '00000000-0000-0000-0000-000000000002','a'*40)
    def build(root):
        start = perf_counter()
        index = engine.build_index(root, **BUILD_PARAMETERS)
        indexed = perf_counter()
        graph = build_code_graph(index, binding=binding)
        end = perf_counter()
        return index, graph, (indexed-start, end-indexed, end-start)

    # A dedicated new temporary tree; no pre-existing Snapshot or Workspace touched.
    temp_parent = ROOT/'.pytest_tmp_ft05_benchmark'
    temp_parent.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix='run-', dir=temp_parent) as temporary:
        for corpus, cases in dataset.items():
            roots = []
            for case in cases:
                root = Path(temporary)/corpus/case['id']
                for path, source in case['files'].items():
                    dest = root/path
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(source, encoding='utf-8')
                roots.append(root)
            labelled, all_sites = [], []
            for case, root in zip(cases, roots):
                index, graph, _ = build(root)  # Warmup plus actual predictions.
                chunks = build_chunk_index(index, binding)
                validate_graph(graph, binding, chunks)
                assert graph_from_dict(graph_to_dict(graph)) == graph
                scopes = {s['id']:s['key'] for s in graph.scopes}
                calls = [dict(path=s['path'], scope=scopes[s['scope_id']], line=s['start_line'],
                    column=s['col_offset'], expression=s['expression'], resolution=s['resolution'],
                    basis=s['basis'], target_id=s['target_id'], candidate_ids=s['candidate_ids'],
                    binding_chain=s['binding_chain'], flow_chain=s['flow_chain'], case=case['id']) for s in graph.call_sites]
                all_sites.extend(calls)
                for label in case['labels']:
                    matches = [s for s in calls if site_key(s) == site_key(label)]
                    if len(matches) != 1:
                        raise ValueError(f"Missing/duplicate labelled call: {case['id']} {label}")
                    labelled.append(dict(**label, case=case['id'], category=case['category'], actual=matches[0]))
                if args.write_legacy and case['id']=='self-is-none':
                    write_json(args.write_legacy, graph_to_dict(replace(graph, project_path='f_t05/self-is-none')))
                queries = {'self-is-none':('callees','method:a.py:C.run'),
                           'alias-identity':('callers','method:a.py:C.work'),
                           'nested-pure-projection':('callees','method:a.py:C.run')}
                if case['id'] in queries:
                    kind, selector = queries[case['id']]
                    context = RetrievalContext.create(root,index,graph,binding=binding,chunks=chunks)
                    answer = query_code_graph(ToolContext(root,retrieval_context=context),kind,selector)
                    assert answer.ok
                    result['queries'].append(dict(case=case['id'], kind=kind, selector=selector,
                        confirmed_calls=sum(e['type']=='calls' for e in answer.data['edges']),
                        confirmed_references=sum(e['type']=='references' for e in answer.data['edges']),
                        flow_evidence_sites=sum(bool(s.get('flow_evidence')) for s in answer.data['call_sites']),
                        edges=answer.data['edges'], call_sites=answer.data['call_sites']))
            timings = []
            for _ in range(args.repeats):
                gc.collect()
                totals = [0.,0.,0.]
                for root in roots:
                    index, graph, elapsed = build(root)
                    totals = [a+b for a,b in zip(totals,elapsed)]
                    del index, graph
                timings.append(totals)
            peaks = []
            for _ in range(3):
                gc.collect()
                tracemalloc.start()
                for root in roots:
                    index, graph, _ = build(root)
                    del index, graph
                peaks.append(tracemalloc.get_traced_memory()[1])
                tracemalloc.stop()
            result['corpora'][corpus] = dict(labelled_sites=labelled, all_sites=all_sites,
                performance={**{key:percentiles([t[i] for t in timings])
                    for i,key in enumerate(('index_seconds','graph_seconds','total_seconds'))},
                    'peak_python_bytes':dict(samples=peaks, maximum=max(peaks))})
    result['summaries'] = recompute(result)
    return result


def compare(before, after):
    for key in ('dataset_sha256','build_parameters','environment','baseline_commit'):
        if before[key] != after[key]:
            raise ValueError('Non-comparable runs: ' + key)
    if before['methodology'] != after['methodology']:
        raise ValueError('Measurement methodology changed')
    changes = []
    for corpus in before['corpora']:
        left, right = (r['corpora'][corpus]['labelled_sites'] for r in (before,after))
        if [{k:v for k,v in s.items() if k!='actual'} for s in left] != [{k:v for k,v in s.items() if k!='actual'} for s in right]:
            raise ValueError('Ground truth changed')
        for a,b in zip(left,right):
            if a['actual'] != b['actual']:
                changes.append(dict(corpus=corpus, case=a['case'], key=site_key(a), expected=a['target_id'],
                                    before=a['actual'], after=b['actual']))
    return dict(dataset_sha256=before['dataset_sha256'], before=recompute(before), after=recompute(after),
                changed_labelled_sites=changes,
                performance={name:dict(before=before['corpora'][name]['performance'],
                                      after=after['corpora'][name]['performance']) for name in before['corpora']},
                queries=dict(before=before['queries'], after=after['queries']))


def export_baseline(destination, commit):
    """Export immutable Python Git objects without checkout, worktree or reset."""
    if len(commit) != 40 or any(c not in '0123456789abcdef' for c in commit):
        raise ValueError('A full lowercase Git commit SHA is required')
    resolved = subprocess.check_output(['git','rev-parse','--verify',commit+'^{commit}'],cwd=ROOT).decode().strip()
    if resolved != commit:
        raise ValueError('Commit mismatch')
    if destination.exists():
        raise FileExistsError('Use a new export directory; existing data is never overwritten')
    names = subprocess.check_output(['git','ls-tree','-r','--name-only',commit,'pycode'],cwd=ROOT).decode().splitlines()
    for name in names:
        if name.endswith('.py'):
            target = destination/name
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(subprocess.check_output(['git','show',f'{commit}:{name}'],cwd=ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--implementation', type=Path, default=ROOT)
    parser.add_argument('--base-commit', default=BASELINE_COMMIT)
    parser.add_argument('--label', default='working-tree')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--repeats', type=int, default=7)
    parser.add_argument('--write-legacy', type=Path)
    parser.add_argument('--summarize', type=Path)
    parser.add_argument('--compare', type=Path, nargs=2)
    parser.add_argument('--export-baseline', type=Path)
    args = parser.parse_args()
    if args.export_baseline:
        export_baseline(args.export_baseline,args.base_commit)
        return
    if args.summarize:
        result = recompute(read_json(args.summarize))
    elif args.compare:
        result = compare(*(read_json(p) for p in args.compare))
    else:
        if args.repeats < 5 or not args.output:
            parser.error('Evaluation needs --output and at least five timing repetitions')
        result = evaluate(args)
    if args.output:
        write_json(args.output, result)
    print(json.dumps(result.get('summaries',result) if not args.compare else
                     dict(changed_labelled_sites=len(result['changed_labelled_sites'])), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
