"""Fixed-corpus ablation accounting, not a real semantic quality benchmark."""
import json
import math
import socket

import pytest

from scripts.benchmarks import a02_retrieval as bench


def item(path, lines):
    return {'path': path, 'displayed_lines': lines}


def test_metrics_hand_calculation_counts_evidence_positions_and_duplicate_lines():
    evidence = [{'path': 'a.py', 'start_line': 2, 'end_line': 3},
                {'path': 'b.py', 'start_line': 1, 'end_line': 1}]
    items = [item('a.py', [2]), item('a.py', [2, 3]), item('irrelevant.py', [1]), item('b.py', [1])]
    score = bench.metrics(items, evidence, 3)
    assert score['file_recall'] == .5 and score['file_mrr'] == 1
    assert score['file_ndcg'] == pytest.approx(1 / (1 + 1 / math.log2(3)))
    assert score['line_coverage'] == pytest.approx(2 / 3)
    assert score['symbol_recall'] == .5
    assert score['duplicate_line_rate'] == .25 and score['duplicate_file_rate'] == pytest.approx(1 / 3)
    assert bench.metrics(items, evidence, 4)['file_recall'] == 1


def test_unknown_and_empty_results_have_distinct_denominators():
    unknown = bench.metrics([item('a.py', [1])], [], 1)
    assert unknown['file_recall'] is None and unknown['line_coverage'] is None
    known = bench.metrics([], [{'path': 'a.py', 'start_line': 1, 'end_line': 1}], 1)
    assert all(known[k] == 0 for k in ('file_recall', 'file_mrr', 'file_ndcg', 'line_coverage', 'symbol_recall'))
    assert known['duplicate_line_rate'] == 0
    with pytest.raises(ValueError):
        bench.metrics([], [], 0)


def test_a00_distinct_file_rank_is_preserved_separately_from_evidence_position_rank():
    records = [{**item(p, [1]), 'snippet': '1: source'} for p in ('a.py','a.py','unlabelled.py','b.py')]
    truth = [{'path': p, 'start_line': 1, 'end_line': 1} for p in ('a.py','b.py')]
    assert bench.metrics(records, truth, 3)['file_recall'] == .5
    assert bench.legacy_metrics(records, truth, 3)['recall'] == 1
    assert bench.legacy_metrics(records, truth, 3)['snippet_line_coverage'] == 1


@pytest.mark.parametrize('options', [
    {'repeats': 0}, {'warmup': -1}, {'top_k': 0}, {'candidate_limit': 21},
    {'candidate_limit': 3, 'top_k': 6}, {'budget': 4095}, {'repeats': True},
])
def test_invalid_configuration_never_reads_dataset_or_creates_output(tmp_path, options):
    out = tmp_path / 'unused'
    with pytest.raises(ValueError):
        bench.run_benchmark(bench.ROOT, tmp_path / 'absent.json', out, **options)
    assert not out.exists()


def test_fake_hash_vectors_are_deterministic_and_know_no_ground_truth():
    a, b = bench.HashFakeProvider(), bench.HashFakeProvider()
    assert a.embed_query('unseen multilingual 查询') == b.embed_documents(['unseen multilingual 查询'])
    assert a.embed_query('').vectors[0][0] == 1
    assert a.provider == 'fake' and a.dimension == 64


def test_ground_truth_is_checked_before_output_and_without_mutating_fixture(tmp_path, monkeypatch):
    source = b'def run():\n    return 1\n'
    dataset = {'schema_version': 1, 'commit': 'a' * 40, 'corpus_prefixes': ['pycode/', 'backend/'],
               'cases': [{'id': 'Q', 'mode': 'ask', 'question': 'run', 'truth_status': 'source_verified',
                          'evidence': [{'path': 'pycode/a.py', 'symbol': 'run', 'start_line': 1,
                                        'end_line': 2, 'source_sha256': '0' * 64}]}]}
    path = tmp_path / 'invalid.json'
    path.write_text(json.dumps(dataset), encoding='utf-8')
    monkeypatch.setattr(bench, 'read_corpus', lambda *a: {'pycode/a.py': source})
    with pytest.raises(ValueError, match='hash mismatch'):
        bench.run_benchmark(bench.ROOT, path, tmp_path / 'out')
    assert not (tmp_path / 'out').exists()


@pytest.fixture(scope='module')
def pinned_report(tmp_path_factory):
    output = tmp_path_factory.mktemp('a02-measurement') / 'new-run'
    with pytest.MonkeyPatch.context() as patch:
        def forbidden(*args, **kwargs):
            pytest.fail('The benchmark must not discover credentials or create a model client')
        patch.setattr('pycode.llm_client._parse_env_file', forbidden)
        patch.setattr('pycode.engine.OpenAIResponsesClient', forbidden)
        result = bench.run_benchmark(bench.ROOT, bench.DATASET_DIR / 'questions.json', output,
                                     repeats=1, warmup=0)
    return result, output


def test_pinned_full_dataset_four_routes_share_binding_budget_and_offline_boundary(pinned_report):
    report, output = pinned_report
    dataset = json.loads((bench.DATASET_DIR / 'questions.json').read_text(encoding='utf-8'))
    assert report['commit'] == dataset['commit'] == '5925bb30fb674451b1c9d08360ca83d00f64780a'
    assert report['dataset_sha256'] == bench.sha256((bench.DATASET_DIR / 'questions.json').read_bytes())
    assert report['total_cases'] == len(dataset['cases'])
    assert report['scored_cases'] == sum(c['truth_status'] == 'source_verified' for c in dataset['cases'])
    assert report['paid_requests'] == report['paid_cost'] == report['environment']['network_calls'] == 0
    assert report['real_model_cost'] is None and report['model_input_tokens'] is None
    assert set(report['summary']) == set(bench.STRATEGIES)
    assert report['configuration']['candidate_limit_per_channel_and_ablation_pool'] == 20
    for row in report['rows']:
        for name, strategy in row['strategies'].items():
            assert len(strategy['samples']) == 1 and not strategy['warmup_samples']
            assert strategy['pack']['estimated_tokens'] <= 12000
            assert len(strategy['selected']) <= 6
            if name != 'production_hybrid':
                assert len(strategy['raw']) <= 20 and strategy['intent'] == 'general'
            if name == 'graph':
                assert all(i['source'] == 'graph' and i['graph_path'] for i in strategy['raw'])
                assert strategy['graph_stats']['edge_checks'] <= 128
            for i in strategy['raw']:
                assert (i['project_id'], i['snapshot_id'], i['commit_sha']) == tuple(
                    report['binding'][k] for k in ('project_id', 'snapshot_id', 'commit_sha'))
                if i['snippet']:
                    lines = (output / 'corpus' / i['path']).read_text(encoding='utf-8-sig').split('\n')
                    assert i['displayed_lines'] == list(range(i['start_line'], i['end_line'] + 1))
                    assert i['snippet'] == '\n'.join(f'{n}: {lines[n-1]}' for n in i['displayed_lines'])
    assert json.loads((output / 'results.json').read_text(encoding='utf-8')) == report
    for name, digest in report['artifact_files'].items():
        assert bench.sha256((output / 'fake-artifact' / name).read_bytes()) == digest


def test_all_raw_selected_metrics_and_aggregate_recompute_exactly(pinned_report):
    report, _ = pinned_report
    for row in report['rows']:
        for strategy in row['strategies'].values():
            for phase in ('raw', 'selected'):
                for k in report['configuration']['ks']:
                    assert strategy[phase + '_metrics'][str(k)] == bench.metrics(strategy[phase], row['evidence'], k)
                    assert strategy['a00_file_metrics'][phase][str(k)] == bench.legacy_metrics(strategy[phase], row['evidence'], k)
    assert bench.aggregate(report['rows'], report['configuration']['ks']) == report['summary']


def test_existing_run_cannot_be_overwritten(pinned_report):
    _, output = pinned_report
    before = bench.sha256((output / 'results.json').read_bytes())
    with pytest.raises(FileExistsError):
        bench.run_benchmark(bench.ROOT, bench.DATASET_DIR / 'questions.json', output)
    assert bench.sha256((output / 'results.json').read_bytes()) == before


def test_offline_network_guard_refuses_a_provider_attempt(tmp_path, monkeypatch):
    source = b'def run():\n    return 1\n'
    dataset = {'schema_version': 1, 'commit': 'a' * 40, 'corpus_prefixes': ['pycode/', 'backend/'],
               'cases': [{'id': 'Q', 'mode': 'ask', 'target': None, 'question': 'run',
                          'truth_status': 'source_verified', 'evidence': [{'path': 'pycode/a.py',
                          'symbol': 'run', 'start_line': 1, 'end_line': 2, 'source_sha256': bench.sha256(source)}]}]}
    path = tmp_path / 'tiny.json'
    path.write_text(json.dumps(dataset), encoding='utf-8')
    monkeypatch.setattr(bench, 'read_corpus', lambda *a: {'pycode/a.py': source})
    def attempted_request(*args):
        with socket.socket() as client:
            client.connect(('127.0.0.1', 1))
    monkeypatch.setattr(bench.HashFakeProvider, 'embed_query', attempted_request)
    with pytest.raises(RuntimeError, match='Network is forbidden'):
        bench.run_benchmark(bench.ROOT, path, tmp_path / 'attempt', repeats=1, warmup=0)


def test_repeat_determinism_and_warmup_accounting_with_small_corpus(tmp_path, monkeypatch):
    source = b'def run():\n    return 1\n\ndef other():\n    return run()\n'
    dataset = {'schema_version': 1, 'commit': 'a' * 40, 'corpus_prefixes': ['pycode/', 'backend/'],
               'cases': [{'id': 'Q', 'mode': 'ask', 'target': None, 'question': 'return result',
                          'truth_status': 'source_verified', 'evidence': [{'path': 'pycode/a.py',
                          'symbol': 'run', 'start_line': 1, 'end_line': 2, 'source_sha256': bench.sha256(source)}]}]}
    path = tmp_path / 'tiny.json'
    path.write_text(json.dumps(dataset), encoding='utf-8')
    monkeypatch.setattr(bench, 'read_corpus', lambda *a: {'pycode/a.py': source})
    report = bench.run_benchmark(bench.ROOT, path, tmp_path / 'repeats', repeats=2, warmup=1)
    assert report['summary']['vector']['query_calls'] == 2
    assert report['summary']['vector']['warmup_query_calls'] == 1
    assert report['summary']['keyword']['query_calls'] == 0
    assert all(len(s['samples']) == 2 and len(s['warmup_samples']) == 1
               for s in report['rows'][0]['strategies'].values())
