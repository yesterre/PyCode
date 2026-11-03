"""Deterministic rank fusion and bounded stored-relation candidate expansion."""
from dataclasses import replace
import math

from pycode.query import RetrievalGraph
from pycode.retrieval_evidence import SourceScore


def identity(spec):
    path, _, _, _, span, _ = spec
    return (path, span.symbol_start_line if span else 0,
            span.symbol_end_line if span else 0, span.symbol_key if span else '')


def fuse(keyword, vector, context):
    """RRF k=60, Keyword/Vector weight 1, unverified static Graph weight .5."""
    graph_specs, graph_paths, stats = [], {}, None
    seeds = []
    view = context.graph_view
    if context.graph is not None:
        if view is None or view.graph is not context.graph:
            view = RetrievalGraph.create(context.graph)
        for spec in [*keyword[:4], *vector[:4]]:
            path, _, _, _, span, _ = spec
            nodes = view.symbols.get((path, span.qualified_name), ()) if span else ()
            seeds.extend(nodes[:1] or ([f'file:{path}'] if f'file:{path}' in view.nodes else []))
        hits, stats = view.expand(seeds, depth=context.graph_depth, node_limit=context.graph_nodes,
                                  edge_limit=context.graph_edge_checks, direction=context.graph_direction)
        spans = {}
        for c in context.chunks.chunks if context.chunks else ():
            spans.setdefault((c.path, c.span.qualified_name), {})[c.span.symbol_key] = c.span
        known_paths = {f.path for f in context.index.files}
        for node, relation_path in hits:
            if node.path not in known_paths:
                continue
            choices = spans.get((node.path, node.name), {})
            span = next(iter(choices.values())) if len(choices) == 1 else None
            role = 'call_hint' if relation_path[-1]['relation'] == 'calls' else 'dependency'
            spec = (node.path, 'graph', 'stored_static_relation_unverified', 1 / len(relation_path), span, role)
            key = identity(spec)
            if key not in graph_paths:
                graph_paths[key] = relation_path
                graph_specs.append(spec)
    channels = (('keyword', keyword, 1.0), ('vector', vector, 1.0), ('graph', graph_specs, .5))
    merged, scores, pinned = {}, {}, set()
    for source, specs, weight in channels:
        unique = {}
        for spec in specs:
            if not math.isfinite(spec[3]):
                raise ValueError('Nonfinite candidate score')
            key = identity(spec)
            if key not in unique or spec[3] > unique[key][3]:
                unique[key] = spec
        ordered = sorted(unique, key=lambda key: (-unique[key][3], key))[:20]
        for rank, key in enumerate(ordered, 1):
            spec = unique[key]
            merged.setdefault(key, spec)
            scores.setdefault(key, []).append(SourceScore(source, float(spec[3]), rank, weight / (60 + rank)))
            if spec[1] in {'exact_path', 'exact_symbol'}:
                pinned.add(key)
    order = sorted(merged, key=lambda key: (key not in pinned, -sum(s.contribution for s in scores[key]), key))
    # Keep exact definitions first; among the remainder expose the best evidence
    # from each file before allowing one file to fill the remaining slots.
    front, rest, files = [], [], {key[0] for key in pinned}
    for key in order:
        if key in pinned or key[0] not in files:
            front.append(key)
            files.add(key[0])
        else:
            rest.append(key)
    items = []
    for key in front + rest:
        path, source, reason, _, span, role = merged[key]
        if path.startswith('tests/') or path.rsplit('/', 1)[-1].startswith('test_'):
            role = 'test'
        contributions = tuple(scores[key])
        reason = ('exact_match_priority;' if key in pinned else '') + 'rrf_k60;' + ';'.join(
            f'{s.source}_rank={s.rank}' for s in contributions)
        item = context.item(path, source=source if key in pinned else 'hybrid', reason=reason,
                            span=span, score=sum(s.contribution for s in contributions), role=role, include_graph=False)
        relation_path = graph_paths.get(key, ())
        if relation_path:
            item = replace(item, reason=item.reason + ';stored_static_relation_unverified')
        items.append(replace(item, source_scores=contributions, graph_path=relation_path,
                             node_ids=(f'file:{path}',),
                             edges=tuple(f"{s['source']} --{s['relation']}--> {s['target']}" for s in relation_path)))
    return items, stats
