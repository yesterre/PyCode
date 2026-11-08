"""Offline A02 ablation. Synthetic vectors test plumbing, never model quality.

python -m scripts.benchmarks.a02_retrieval --output-dir <new directory>
No service/database, developer .env or model client is used. Artifacts and the
pinned Git corpus are retained in a new directory, never overwriting a run.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import re
import socket
import statistics
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from scripts.benchmarks.a00_baseline import (
    DATASET_DIR, ROOT, git, materialize, read_corpus, score as a00_score, sha256, validate_dataset,
)
from pycode.chunks import SnapshotBinding, build_chunk_index, save_chunk_index
from pycode.embedding import EmbeddingResult, prepare_inputs
from pycode.engine import PyCodeEngine
from pycode.evidence_pack import build_pack
from pycode.graph_builder import build_code_graph
from pycode.hybrid_retrieval import fuse
from pycode import retriever
from pycode.retrieval_evidence import Query, RetrievalContext, RetrievalRequest, RetrievalResult
from pycode.semantic_index import SemanticIdentity, encode_vectors, seal_artifact
from pycode.semantic_search import ReadOnlyVectorIndex
from pycode.utils import normalize_path


STAGE = ROOT / 'docs_v3.0/v3_agent/A02/A02b/A02b3'
STRATEGIES = ('keyword', 'vector', 'graph', 'hybrid', 'production_hybrid')


class HashFakeProvider:
    """Ground-Truth-blind signed token hashing; NOT a semantic model."""
    provider, model, dimension = 'fake', 'sha256-token-hash-v1', 64

    def embed_documents(self, texts):
        rows = []
        for text in texts:
            row = [0.0] * self.dimension
            row[0] = 1.0  # Empty/OOV queries still have a valid, nonzero vector.
            for token in re.findall(r'[a-z_][a-z_0-9]*|[\u4e00-\u9fff]', text.lower()):
                digest = hashlib.sha256(token.encode('utf-8')).digest()
                row[1 + int.from_bytes(digest[:4], 'little') % (self.dimension - 1)] += 1 if digest[4] & 1 else -1
            rows.append(row)
        return EmbeddingResult(self.provider, self.model, self.dimension, len(rows), rows)

    def embed_query(self, text):
        return self.embed_documents([text])


def metrics(items, evidence, k):
    """File-rank and necessary-symbol line metrics; unknown is never a zero."""
    if type(k) is not int or k < 1:
        raise ValueError('K must be positive')
    selected = items[:k]  # K is Evidence positions, not K distinct paths.
    paths = list(dict.fromkeys(i['path'] for i in selected))
    counts = Counter((i['path'], line) for i in selected for line in i['displayed_lines'])
    total = sum(counts.values())
    result = {'duplicate_line_rate': (total - len(counts)) / total if total else 0.0,
              'duplicate_file_rate': (len(selected) - len(paths)) / len(selected) if selected else 0.0,
              'returned_items': len(selected)}
    if not evidence:
        return {**result, 'file_recall': None, 'file_mrr': None, 'file_ndcg': None,
                'line_coverage': None, 'symbol_recall': None}
    relevant = {e['path'] for e in evidence}
    expected = {(e['path'], n) for e in evidence for n in range(e['start_line'], e['end_line'] + 1)}
    # A relevant file earns gain only on its first occurrence, at its actual
    # Evidence rank. Repeated functions in one file cannot inflate DCG or MRR.
    seen, dcg, first = set(), 0.0, None
    for rank, item in enumerate(selected, 1):
        path = item['path']
        if path in relevant and path not in seen:
            dcg += 1 / math.log2(rank + 1)
            first = first or rank
        seen.add(path)
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(k, len(relevant)) + 1))
    symbols = [all((e['path'], n) in counts for n in range(e['start_line'], e['end_line'] + 1))
               for e in evidence]
    return {**result, 'file_recall': len(relevant.intersection(paths)) / len(relevant),
            'file_mrr': 1 / first if first else 0.0, 'file_ndcg': dcg / ideal,
            'line_coverage': len(expected.intersection(counts)) / len(expected),
            'symbol_recall': sum(symbols) / len(symbols)}


def record_item(item):
    value = item.to_dict()
    displayed = []
    for line in item.snippet.split('\n'):
        prefix, separator, _ = line.partition(': ')
        if separator and prefix.isdecimal():
            displayed.append(int(prefix))
    return {**value, 'displayed_lines': displayed,
            'snippet_sha256': sha256(item.snippet.encode('utf-8'))}


def legacy_metrics(items, evidence, k):
    """Unmodified A00 distinct-file rank/coverage definition, kept separately."""
    return a00_score([SimpleNamespace(**i) for i in items], evidence, k)


def distribution(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return {'samples': len(values), 'p50': statistics.median(values),
            'p95': values[math.ceil(.95 * len(values)) - 1], 'mean': statistics.mean(values)}


def aggregate(rows, ks):
    result = {}
    for strategy in STRATEGIES:
        group = [row['strategies'][strategy] for row in rows]
        measures = {}
        for phase in ('raw', 'selected'):
            measures[phase] = {}
            for k in ks:
                samples = [g[phase + '_metrics'][str(k)] for g in group]
                measures[phase][str(k)] = {
                    key: statistics.mean(s[key] for s in samples if s[key] is not None)
                    if any(s[key] is not None for s in samples) else None for key in samples[0]}
        measures['a00_file_metrics'] = {}
        for phase in ('raw', 'selected'):
            measures['a00_file_metrics'][phase] = {}
            for k in ks:
                samples = [g['a00_file_metrics'][phase][str(k)] for g in group]
                measures['a00_file_metrics'][phase][str(k)] = {
                    key: statistics.mean(s[key] for s in samples if s[key] is not None)
                    if any(s[key] is not None for s in samples) else None for key in samples[0]}
        measures['latency_ms'] = {key: distribution([s[key] for g in group for s in g['samples']])
                                  for key in group[0]['samples'][0] if key.endswith('_ms')}
        measures['query_calls'] = sum(s['query_calls'] for g in group for s in g['samples'])
        measures['query_input_utf8_bytes'] = sum(s['query_input_utf8_bytes'] for g in group for s in g['samples'])
        measures['warmup_query_calls'] = sum(s['query_calls'] for g in group for s in g['warmup_samples'])
        measures['warmup_query_input_utf8_bytes'] = sum(s['query_input_utf8_bytes'] for g in group for s in g['warmup_samples'])
        measures['evidence_estimated_tokens'] = distribution([g['pack']['estimated_tokens'] for g in group])
        measures['discard_reasons'] = dict(sum((Counter(g['pack']['reason_counts']) for g in group), Counter()))
        measures['empty_cases'] = sum(not g['selected'] for g in group)
        measures['unknown_cases_returning_evidence'] = sum(
            row['truth_status'] == 'unknown' and bool(row['strategies'][strategy]['selected']) for row in rows)
        result[strategy] = measures
    return result


def _captured_candidates(request, context):
    # Offline, single-threaded instrumentation only. Bypass selection to observe
    # the production candidate order, then apply ONE common cap/Pack below.
    # No copy of Hybrid's candidate generation or ranking is maintained here.
    with patch.object(retriever, '_pack', side_effect=lambda result, *_: result):
        return retriever.retrieve(request, context)


def _evaluate(strategy, case, context, vector_index, provider, *, cap, top_k, budget):
    clock = perf_counter()
    costs = {'query_calls': 0, 'query_input_utf8_bytes': 0, 'query_embedding_ms': 0.0, 'vector_search_ms': 0.0}

    def query(text, limit):
        started = perf_counter()
        response = provider.embed_query(text)
        costs['query_embedding_ms'] += (perf_counter() - started) * 1000
        costs['query_calls'] += 1
        costs['query_input_utf8_bytes'] += len(text.encode('utf-8'))
        started = perf_counter()
        value = vector_index.search(response, limit)
        costs['vector_search_ms'] += (perf_counter() - started) * 1000
        return value

    configured = replace(context, vector_query=query, vector_top_k=cap, token_budget=budget)
    request = RetrievalRequest(Query(case['question'], 'general'), cap)
    if strategy == 'vector':
        hits = query(case['question'], cap).hits
        items = [context.item(h.path, source='vector', reason='cosine_max_over_definition_segments',
                              score=h.score, span=h.span if h.span.symbol_kind != 'module' else None,
                              include_graph=False) for h in hits]
        raw = RetrievalResult(case['question'], 'general', items, context.binding, mode='vector')
    elif strategy == 'graph':
        # Graph cannot interpret text independently. Match production Hybrid's
        # first four lexical seeds, then retain ONLY its graph-channel hits.
        seeds = retriever._lexical_candidates(case['question'], context)[:cap]
        items, stats = fuse(seeds, [], configured)
        graph_items = []
        for item in items:
            score = next((s for s in item.source_scores if s.source == 'graph'), None)
            if score:
                graph_items.append(replace(item, source='graph', score=score.score, source_scores=(score,),
                                           reason='graph_channel_ablation;stored_static_relation_unverified'))
        graph_items.sort(key=lambda i: i.source_scores[0].rank)
        raw = RetrievalResult(case['question'], 'general', graph_items, context.binding,
                              mode='hybrid', graph_stats=stats)
    elif strategy == 'production_hybrid':
        # Additional production retrieval replay; intent/explicit impact target
        # are preserved. It is deliberately outside the four-channel ablation.
        actual = RetrievalRequest(Query(case['question'], 'impact', case['target'])
                                  if case['mode'] == 'impact' else Query(case['question']), top_k)
        captured = []
        original = retriever._pack
        def observe(result, *args):
            captured.append(result)
            return original(result, *args)
        with patch.object(retriever, '_pack', side_effect=observe):
            selected = retriever.retrieve(actual, replace(configured, retrieval_mode='hybrid'))
        elapsed = (perf_counter() - clock) * 1000
        costs.update(total_ms=elapsed, candidate_ms=None, pack_ms=None)
        return captured[0], selected, costs
    else:
        raw = _captured_candidates(request, replace(configured, retrieval_mode=strategy))
    raw = replace(raw, items=raw.items[:cap])
    candidate_ms = (perf_counter() - clock) * 1000
    started = perf_counter()
    # All four ablations share Hybrid's overlap/file-diversity Pack policy.
    # Production replay above keeps its original policy and candidate pool.
    selected = build_pack(replace(raw, mode='hybrid'), budget, top_k)
    costs.update(candidate_ms=candidate_ms, pack_ms=(perf_counter() - started) * 1000,
                 total_ms=(perf_counter() - clock) * 1000)
    return raw, selected, costs


def _offline(*args, **kwargs):
    raise RuntimeError('Network is forbidden in the offline A02 benchmark')


def run_benchmark(repo, dataset_path, output_dir, *, repeats=3, warmup=1,
                  candidate_limit=20, top_k=6, budget=12000, structured_graph=False):
    run_started = perf_counter()
    if (type(repeats) is not int or repeats < 1 or type(warmup) is not int or warmup < 0
            or type(candidate_limit) is not int or type(top_k) is not int
            or not 1 <= top_k <= candidate_limit <= 20
            or type(budget) is not int or not 4096 <= budget <= 1048576
            or type(structured_graph) is not bool):
        raise ValueError('Invalid benchmark configuration')
    # Validate Git and Ground Truth BEFORE creating or writing output.
    data = Path(dataset_path).read_bytes()
    dataset = json.loads(data)
    corpus = read_corpus(Path(repo), dataset['commit'])
    validate_dataset(dataset, corpus)
    corpus_hashes = {path: sha256(source) for path, source in corpus.items()}
    corpus_hash = sha256(json.dumps(corpus_hashes, sort_keys=True).encode())
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    root = output_dir / 'corpus'
    root.mkdir()
    materialize(corpus, root)
    with patch.object(socket.socket, 'connect', _offline), patch.object(socket, 'getaddrinfo', _offline):
        started = perf_counter()
        index = PyCodeEngine().build_index(root, include_chunk_metadata=True,
            **({'include_graph_metadata': True} if structured_graph else {}))
        # Git corpus paths are POSIX. Canonicalize the host's FileInfo separators
        # in this offline harness, before building either Graph or Chunk IDs.
        for file in index.files:
            file.path = normalize_path(file.path)
        binding = SnapshotBinding(str(uuid5(NAMESPACE_URL, 'pycode-a02-offline')),
                                  str(uuid5(NAMESPACE_URL, dataset['commit'] + corpus_hash)), dataset['commit'])
        graph = build_code_graph(index, **({'binding': binding} if structured_graph else {}))
        chunks = build_chunk_index(index, binding)
        chunk_path = output_dir / 'chunks.json'
        save_chunk_index(chunks, chunk_path)
        context = RetrievalContext.create(root, index, graph, binding=binding, chunks=chunks)
        index_ms = (perf_counter() - started) * 1000
        provider = HashFakeProvider()
        started = perf_counter()
        segments = prepare_inputs(chunks)
        response = provider.embed_documents([s.text for s in segments])
        directory = output_dir / 'fake-artifact'
        directory.mkdir()
        (directory / 'vectors.f32').write_bytes(encode_vectors(response.vectors, provider.dimension))
        target = SemanticIdentity(binding, sha256(chunk_path.read_bytes()), provider.provider, provider.model, provider.dimension)
        seal_artifact(directory, target, segments)
        embedding_ms = (perf_counter() - started) * 1000
        started = perf_counter()
        vector_index = ReadOnlyVectorIndex.load(directory, target, chunks)
        load_ms = (perf_counter() - started) * 1000
        rows, ks = [], sorted({1, min(3, top_k), top_k})
        for case_index, case in enumerate(dataset['cases']):
            records = {s: {'samples': [], 'warmup_samples': []} for s in STRATEGIES}
            for repeat in range(-warmup, repeats):
                offset = (case_index + repeat + warmup) % len(STRATEGIES)
                for strategy in STRATEGIES[offset:] + STRATEGIES[:offset]:
                    raw, selected, costs = _evaluate(strategy, case, context, vector_index, provider,
                                                     cap=candidate_limit, top_k=top_k, budget=budget)
                    if repeat < 0:
                        records[strategy]['warmup_samples'].append(costs)
                        continue
                    row = records[strategy]
                    raw_items = [record_item(i) for i in raw.items]
                    selected_items = [record_item(i) for i in selected.items]
                    signature = sha256(json.dumps([raw_items, selected_items], sort_keys=True, ensure_ascii=False).encode())
                    if row.get('signature', signature) != signature:
                        raise ValueError('Nondeterministic retrieval in identical repeats')
                    row.update(signature=signature, raw=raw_items, selected=selected_items,
                               intent=raw.intent, diagnostics=list(raw.diagnostics), graph_stats=raw.graph_stats,
                               pack=selected.pack.summary(),
                               a00_file_metrics={phase: {str(k): legacy_metrics(items, case['evidence'], k) for k in ks}
                                                 for phase, items in (('raw', raw_items), ('selected', selected_items))},
                               raw_metrics={str(k): metrics(raw_items, case['evidence'], k) for k in ks},
                               selected_metrics={str(k): metrics(selected_items, case['evidence'], k) for k in ks})
                    row['samples'].append(costs)
            rows.append({'id': case['id'], 'mode': case['mode'], 'truth_status': case['truth_status'],
                         'evidence': case['evidence'], 'strategies': records})
    implementation = {p.relative_to(ROOT).as_posix(): sha256(p.read_bytes())
                      for p in sorted((ROOT / 'pycode').rglob('*.py'))}
    result = {
        'schema_version': 1, 'created_at': datetime.now(timezone.utc).isoformat(),
        'commit': dataset['commit'], 'binding': asdict(binding),
        'snapshot_kind': 'offline synthetic identity; no database Snapshot or user Artifact',
        'dataset_sha256': sha256(data), 'corpus_sha256': corpus_hash, 'corpus_files': corpus_hashes,
        'implementation_files': implementation, 'runner_sha256': sha256(Path(__file__).read_bytes()),
        'implementation_head': git(ROOT, 'rev-parse', 'HEAD').decode().strip(),
        'artifact_files': {p.name: sha256(p.read_bytes()) for p in sorted(directory.iterdir())},
        'environment': {'python': platform.python_version(), 'os': platform.platform(), 'network_calls': 0,
                        'provider': provider.provider, 'model': provider.model, 'dimension': provider.dimension},
        'configuration': {'repeats': repeats, 'warmup': warmup, 'candidate_limit_per_channel_and_ablation_pool': candidate_limit,
                          'structured_graph': structured_graph, 'graph_schema_version': graph.schema_version,
                          'top_k': top_k, 'ks': ks, 'budget': budget, 'graph_depth': context.graph_depth,
                          'graph_nodes': context.graph_nodes, 'graph_edge_checks': context.graph_edge_checks,
                          'graph_direction': context.graph_direction, 'order': 'cyclic rotation, no query cache',
                          'ablation_intent': 'general; no explicit target, original question text unchanged',
                          'ablation_pack_policy': 'common hybrid overlap and file-diversity rules',
                          'graph_seeds': 'production fuse first four keyword candidates; no oracle/Vector seeds',
                          'production_replay': 'original inferred intent/impact target; unmodified production Pack/pool',
                          'estimator': 'utf8_bytes_upper_bound_v1; evidence projection only, not entire Agent request'},
        'metric_policy': {'a00_file_metrics': 'unchanged A00 distinct-file rank and displayed-line coverage',
                          'raw_metrics_and_selected_metrics': 'Evidence-position K, binary first-file gain nDCG, union line/symbol coverage',
                          'positive_metrics': 'macro mean over 15 source_verified cases; unknown excluded',
                          'duplication_and_costs': 'all cases; unlabelled evidence is not assumed irrelevant'},
        'setup': {'index_graph_chunks_ms': index_ms, 'fake_document_embedding_and_seal_ms': embedding_ms,
                  'validated_artifact_load_ms': load_ms, 'files': len(index.files), 'chunks': len(chunks.chunks),
                  'segments': len(segments), 'document_input_utf8_bytes': sum(len(s.text.encode()) for s in segments),
                  'artifact_bytes': sum(p.stat().st_size for p in directory.iterdir()), 'document_calls': 1},
        'total_cases': len(rows), 'scored_cases': sum(c['truth_status'] == 'source_verified' for c in rows),
        'quality_scope': 'Synthetic plumbing only; no real embedding quality, answer accuracy or groundedness claim.',
        'paid_requests': 0, 'paid_cost': 0, 'real_model_cost': None, 'model_input_tokens': None,
        'elapsed_ms_before_report_serialization': (perf_counter() - run_started) * 1000,
        'summary': aggregate(rows, ks), 'rows': rows,
    }
    (output_dir / 'results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=DATASET_DIR / 'questions.json')
    parser.add_argument('--output-dir', type=Path, default=STAGE / datetime.now(timezone.utc).strftime('experiment-%Y%m%dT%H%M%S%fZ'))
    parser.add_argument('--repeats', type=int, default=3)
    parser.add_argument('--warmup', type=int, default=1)
    parser.add_argument('--candidate-limit', type=int, default=20)
    parser.add_argument('--top-k', type=int, default=6)
    parser.add_argument('--budget', type=int, default=12000)
    parser.add_argument('--structured-graph', action='store_true', help='Explicitly evaluate current bound structural Graph; legacy default retained')
    args = parser.parse_args()
    result = run_benchmark(ROOT, args.dataset, args.output_dir, repeats=args.repeats, warmup=args.warmup,
                           candidate_limit=args.candidate_limit, top_k=args.top_k, budget=args.budget,
                           structured_graph=args.structured_graph)
    print(json.dumps({'output': str(args.output_dir), 'commit': result['commit'],
                      'scored_cases': result['scored_cases'], 'paid_requests': result['paid_requests']}))


if __name__ == '__main__':
    main()
