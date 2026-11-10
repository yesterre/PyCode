"""One selection and conservative rendering budget for Prompt and Tool evidence."""
from collections import Counter
from dataclasses import dataclass, replace
import json


def _render_relations(item):
    """Expose stored proof metadata without treating it as runtime verification."""
    lines=[];documented=set()
    for step in item.graph_path:
        edge=f"{step['source']} --{step['relation']}--> {step['target']}"
        resolution=step.get('resolution')
        if resolution not in {'static','declaration','unknown','ambiguous','legacy'}:
            resolution='unavailable'
        basis=step.get('basis') or 'unavailable'
        # Keep Graph provenance separate from a visible source-code citation.
        location=(f"graph_site_path={step['path']}; graph_site_lines={step['start_line']}-{step['end_line']}"
                  if step.get('path') and step.get('start_line') is not None and step.get('end_line') is not None
                  else 'graph_site=unavailable')
        lines.append(f'- {edge} [resolution={resolution}; basis={basis}; {location}; runtime=unverified]')
        documented.add(edge)
    lines.extend(f'- {edge} [proof=unavailable; runtime=unverified]'
                 for edge in item.edges if edge not in documented)
    return '\n'.join(lines) or '- N/A'


def render_evidence(items):
    blocks = []
    for number, item in enumerate(items, 1):
        blocks.append('\n'.join([
            f'## 证据 {number}: {item.title}', f'路径: {item.path or "N/A"}',
            f'选择原因: {item.reason or "N/A"}',
            f'Snapshot: {item.snapshot_id or "unbound"}; Commit: {item.commit_sha or "unbound"}',
            f'Symbol: {item.symbol or "unknown"}; Lines: {item.start_line}-{item.end_line}; Coverage: {item.coverage}',
            f'Chunk IDs: {", ".join(item.chunk_ids) or "unavailable"}',
            f'Diagnostics: {", ".join(item.diagnostics) or "none"}',
            '节点:', '\n'.join('- ' + n for n in item.node_ids) or '- N/A',
            '关系（图谱出处不能替代可见源码；不证明运行时调用）:', _render_relations(item),
            '代码片段:', item.snippet or 'N/A']))
    return '\n\n'.join(blocks) or '没有检索到可用证据。'


def tool_items(items):
    return [{**item.to_dict(), 'snippet_truncated': False} for item in items]


def encoded(value):
    return json.dumps(value, ensure_ascii=False, indent=2)


@dataclass(frozen=True)
class EvidencePack:
    items: tuple
    budget: int
    estimated_tokens: int
    discarded: tuple
    discarded_count: int
    reason_counts: tuple

    def summary(self):
        return {'budget':self.budget, 'estimated_tokens':self.estimated_tokens,
                'estimator':'utf8_bytes_upper_bound_v1', 'selected_count':len(self.items),
                'discarded_count':self.discarded_count, 'discarded':list(self.discarded),
                'discarded_omitted':self.discarded_count - len(self.discarded),
                'reason_counts':dict(self.reason_counts)}


def measure(result):
    """UTF-8 bytes, not a model tokenizer. Includes repeated citation display.

    This bounds the evidence projection, not question text, unrelated tool data,
    prior Agent turns, planner/memory or the entire model request. 512 bytes are
    reserved for evidence wrapper labels. Both actual renderer shapes are counted.
    """
    projection = {'items':tool_items(result.items), 'evidence':result.evidence, 'retrieval':result.summary()}
    projection['retrieval']['duration_ms'] = 99999999999.999
    projection['retrieval']['status'] = 'unavailable'
    projection['retrieval']['diagnostics'] = list(dict.fromkeys([
        *projection['retrieval']['diagnostics'], 'evidence_budget_limited']))
    tool = encoded(projection) + '\n'.join(result.evidence)
    prompt = render_evidence(result.items) + encoded(result.pack.summary() if result.pack else None)
    return 512 + max(len(tool.encode('utf-8')), len(prompt.encode('utf-8')))


def build_pack(result, budget, limit):
    if type(budget) is not int or not 4096 <= budget <= 1048576:
        raise ValueError('Evidence budget must be between 4096 and 1048576')
    candidates = result.items
    if result.binding is not None and any(
        (i.project_id, i.snapshot_id, i.commit_sha) !=
        (result.binding.project_id, result.binding.snapshot_id, result.binding.commit_sha) for i in candidates
    ):
        raise ValueError('Evidence Pack cannot mix Snapshot bindings')
    rejected = set()
    while True:
        selected, selected_indices, decisions, files = [], [], [], Counter()
        for index, item in enumerate(candidates):
            reason = None
            if index in rejected:
                reason = 'token_budget'
            elif any(s.path == item.path and s.start_line == item.start_line and s.end_line == item.end_line for s in selected):
                reason = 'overlap'
            elif len(selected) >= limit:
                reason = 'item_limit'
            elif result.mode == 'hybrid' and item.source not in {'exact_path','exact_symbol'}:
                if any(s.path == item.path and s.start_line is not None and item.start_line is not None
                       and s.start_line <= item.end_line and item.start_line <= s.end_line for s in selected):
                    reason = 'overlap'
                elif files[item.path] >= 2:
                    reason = 'file_diversity'
            if reason:
                decisions.append({'path':item.path, 'symbol':item.symbol, 'start_line':item.start_line,
                                  'end_line':item.end_line, 'reason':reason})
            else:
                selected.append(item)
                selected_indices.append(index)
                files[item.path] += 1
        reasons = tuple(sorted(Counter(d['reason'] for d in decisions).items()))
        pack = EvidencePack(tuple(selected), budget, budget, tuple(decisions[:20]), len(decisions), reasons)
        proposed = replace(result, items=selected, pack=pack)
        while pack.discarded and measure(proposed) > budget:
            pack = replace(pack, discarded=pack.discarded[:-1])
            proposed = replace(proposed, pack=pack)
        cost = measure(proposed)
        if cost <= budget:
            # Reserving budget's digit width above makes the final estimate an
            # upper bound even after the estimate itself is serialized.
            return replace(proposed, pack=replace(pack, estimated_tokens=cost))
        if not selected:
            raise ValueError('Evidence metadata exceeds minimum budget')
        removable = [j for j in selected_indices if candidates[j].source not in {'exact_path','exact_symbol'}] or selected_indices
        largest = max(removable, key=lambda j: (len(encoded(candidates[j].to_dict()).encode('utf-8')), j))
        rejected.add(largest)
