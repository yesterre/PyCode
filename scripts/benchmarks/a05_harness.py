"""Fixed offline A05 engineering comparison; never a model quality benchmark.

python -m scripts.benchmarks.a05_harness --output-dir <new directory>
Uses read-only Git objects, isolated imports, Fake models and no services.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path, PurePosixPath
import platform
import re
import statistics
import subprocess
import sys
import tarfile


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / 'tests/fixtures/a05/harness_cases.json'
WORKER = Path(__file__).with_name('a05_harness_case.py')
FIXTURE_CANONICAL_SHA256 = 'bf1f85c15b1cab131774cbff4453a1a4ef8548d1f2fd4ec7d0ff59a364cc38f9'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def safe_path(name):
    path = PurePosixPath(name)
    if not name or path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name:
        raise ValueError('Unsafe benchmark source path')
    return path


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT)


def read_fixture(path=FIXTURE):
    data = Path(path).read_bytes()
    value = json.loads(data)
    canonical = sha(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())
    if Path(path).resolve() == FIXTURE.resolve() and canonical != FIXTURE_CANONICAL_SHA256:
        raise ValueError('Fixed scenario data changed; review and version the fixture explicitly')
    if (value.get('schema_version') != 1 or value.get('kind') != 'offline_engineering_scenarios_not_model_quality'
            or not re.fullmatch('[0-9a-f]{40}', value.get('baseline_commit', ''))
            or not re.fullmatch('[0-9a-f]{40}', value.get('corpus_commit', ''))):
        raise ValueError('Unsupported A05 scenario fixture')
    for name in value['corpus_paths']:
        if safe_path(name).suffix != '.py':
            raise ValueError('Expected Python text fixture')
    ids = [case['id'] for case in value['cases']]
    if len(ids) != len(set(ids)) or any(not re.fullmatch('[a-z_]+', name) for name in ids):
        raise ValueError('Invalid or duplicate scenario ID')
    return value, sha(data)


def export_implementation(commit, destination):
    """No checkout/worktree/index changes; reject links and escaping members."""
    archive = git('archive', '--format=tar', commit, 'pycode')
    destination = Path(destination)
    destination.mkdir(exist_ok=False)
    files = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode='r:') as source:
        for member in source.getmembers():
            path = safe_path(member.name)
            if path.parts[0] != 'pycode' or not (member.isfile() or member.isdir()):
                raise ValueError('Unexpected Git archive member')
            if member.isdir():
                continue
            data = source.extractfile(member).read()
            target = destination / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            files[path.as_posix()] = sha(data)
    return dict(commit=commit, archive_sha256=sha(archive), files=files)


def copy_current_implementation(head, destination):
    """Freeze actual development code too; never silently evaluate a stale HEAD."""
    names = (git('ls-files','-z','--','pycode') +
             git('ls-files','-z','--others','--exclude-standard','--','pycode')).decode().split('\0')
    destination = Path(destination)
    destination.mkdir(exist_ok=False)
    files = {}
    for name in sorted(set(names) - {''}):
        path = safe_path(name)
        source = ROOT / name
        if not source.exists():
            continue  # A tracked deletion remains visible in dirty_paths below.
        if source.is_symlink() or not source.resolve().is_relative_to((ROOT / 'pycode').resolve()):
            raise ValueError('Current implementation path escapes source directory')
        data = source.read_bytes()
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files[name] = sha(data)
    return dict(commit=head, representation='working_tree_snapshot', archive_sha256=None,
        dirty_paths=git('status','--porcelain','--untracked-files=all','--','pycode').decode().splitlines(), files=files)


def distribution(values):
    if not values:
        return None
    values = sorted(values)
    return dict(samples=len(values), p50=statistics.median(values),
                p95=values[math.ceil(.95 * len(values)) - 1], mean=statistics.mean(values))


def summarize(rows):
    measured = [r for r in rows if not r['warmup']]
    total = len(measured)
    outcomes = Counter(r['outcome'] for r in measured)
    return dict(samples=total, outcomes=dict(outcomes),
        completion_rate=outcomes['complete'] / total, partial_rate=outcomes['partial'] / total,
        failure_rate=outcomes['failed'] / total,
        runtime_ok_rate=sum(r['runtime_ok'] for r in measured) / total,
        contract_pass_rate=sum(r['contract_passed'] for r in measured) / total,
        stop_reasons=dict(Counter(r['stop_reason'] for r in measured)),
        calls={key:sum(r[key] for r in measured) for key in (
            'model_calls','model_failures','tool_observations','handler_attempts','query_embedding_calls',
            'failed_tool_observations','repeated_failed_observations','no_progress_events','fallback_events')},
        repeated_failure_rate=(sum(r['repeated_failed_observations'] for r in measured) /
            sum(r['failed_tool_observations'] for r in measured)) if any(r['failed_tool_observations'] for r in measured) else 0.0,
        latency_ms=distribution([r['latency_ms'] for r in measured]),
        setup_ms=distribution([r['setup_ms'] for r in measured]),
        estimated_input_tokens=distribution([r['estimated_input_tokens'] for r in measured]),
        peak_estimated_input_tokens=distribution([r['peak_estimated_input_tokens'] for r in measured]),
        discard_reasons=dict(sum((Counter(r['discard_reasons'] or {}) for r in measured), Counter()))
            if any(r['discard_reasons'] is not None for r in measured) else None)


def child_environment():
    result = {k:v for k,v in os.environ.items() if not k.startswith((
        'OPENAI_', 'AZURE_OPENAI_', 'LLM_', 'PYCODE_EMBEDDING_', 'PYCODE_AGENT_'))
        and k not in {'PYTHONPATH','DATABASE_URL','REDIS_URL','TEST_DATABASE_URL','TEST_REDIS_URL'}}
    result.update(PYTHONUTF8='1', PYTHON_DOTENV_DISABLED='1', PYTHONDONTWRITEBYTECODE='1',
                  PYCODE_EMBEDDING_ENABLED='false', PYCODE_RETRIEVAL_MODE='keyword')
    return result


def run_benchmark(output, *, repeats=5, warmups=1, fixture=FIXTURE):
    if type(repeats) is not int or not 1 <= repeats <= 100 or type(warmups) is not int or not 0 <= warmups <= 10:
        raise ValueError('Invalid repeat/warmup count')
    dataset, fixture_hash = read_fixture(fixture)
    head = git('rev-parse', 'HEAD').decode().strip()
    out = Path(output).resolve()
    out.mkdir(parents=True, exist_ok=False)
    started = datetime.now(timezone.utc).isoformat()
    (out / 'fixture.json').write_bytes(Path(fixture).read_bytes())
    corpus = out / 'corpus'
    corpus.mkdir()
    corpus_manifest = {}
    for name in dataset['corpus_paths']:
        data = git('show', dataset['corpus_commit'] + ':' + name)
        path = corpus / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        corpus_manifest[name] = sha(data)
    variants = {}
    for label, commit in (('pre_a05', dataset['baseline_commit']), ('a05_current', head)):
        source = out / (label + '_source')
        manifest = (copy_current_implementation(head, source) if label == 'a05_current'
                    else export_implementation(commit, source))
        result = out / (label + '.json')
        command = [sys.executable, str(WORKER), str(source), str(out / 'fixture.json'), str(corpus),
                   str(result), str(out / (label + '_work')), str(repeats), str(warmups)]
        child = subprocess.run(command, cwd=out, env=child_environment(), capture_output=True,
                               text=True, encoding='utf-8', timeout=600)
        (out / (label + '.log')).write_text(child.stdout + child.stderr, encoding='utf-8')
        if child.returncode:
            raise RuntimeError(f'Offline worker failed: {label}; see its retained log')
        raw = json.loads(result.read_text(encoding='utf-8'))
        variants[label] = dict(source=manifest, command=command, summary=summarize(raw['rows']),
            cases={case['id']:summarize([r for r in raw['rows'] if r['case_id'] == case['id']]) for case in dataset['cases']})
        # No source writes, including accidental bytecode in the exported tree.
        if {p.relative_to(source).as_posix():sha(p.read_bytes()) for p in source.rglob('*') if p.is_file()} != manifest['files']:
            raise RuntimeError('Exported implementation was modified')
    report = dict(schema_version=1, kind=dataset['kind'], started_at_utc=started,
        finished_at_utc=datetime.now(timezone.utc).isoformat(), branch=git('branch','--show-current').decode().strip(),
        entry_head=head, fixture_sha256=fixture_hash, corpus_commit=dataset['corpus_commit'], corpus_files=corpus_manifest,
        driver_sha256=sha(WORKER.read_bytes()), harness_sha256=sha(Path(__file__).read_bytes()),
        repeats=repeats, warmups=warmups, estimator='utf8_bytes_plus_512_not_billed_tokens',
        environment=dict(python=platform.python_version(), platform=platform.platform(),
            execution='sequential subprocesses; process/import/setup excluded from runtime latency',
            real_models=False, network='blocked_in_workers', dotenv=False), variants=variants,
        limitations=['Synthetic scripted models do not measure answer accuracy.',
            'Measured wall latency excludes real providers and does not establish production performance.',
            'Legacy missing reports are null; desired unsupported limits are not retrospectively enforced.',
            'Completion rates describe this fixed engineering case mix, not production task success.'])
    (out / 'summary.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    with (out / 'comparison.csv').open('x', encoding='utf-8', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['case','variant','contract_pass_rate','completion_rate','partial_rate','failure_rate',
                         'model_calls','handler_attempts','latency_p50_ms','latency_p95_ms','input_estimate_mean'])
        for case in dataset['cases']:
            for label, variant in variants.items():
                row = variant['cases'][case['id']]
                writer.writerow([case['id'],label,*[row[k] for k in ('contract_pass_rate','completion_rate','partial_rate','failure_rate')],
                    row['calls']['model_calls'],row['calls']['handler_attempts'],row['latency_ms']['p50'],
                    row['latency_ms']['p95'],row['estimated_input_tokens']['mean']])
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--warmups', type=int, default=1)
    args = parser.parse_args()
    report = run_benchmark(args.output_dir, repeats=args.repeats, warmups=args.warmups)
    print(json.dumps({label:value['summary'] for label,value in report['variants'].items()}, indent=2))
    if report['variants']['a05_current']['summary']['contract_pass_rate'] != 1:
        raise SystemExit('Current implementation violates fixed engineering scenario expectations; raw results retained')


if __name__ == '__main__':
    main()
