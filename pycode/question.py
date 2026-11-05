"""Question rules and trusted Snapshot preflight, reusing the existing Retriever."""
from dataclasses import dataclass, field
import hashlib
import json
import re

POLICY_VERSION = 'a04b-v1'
INTENTS = ('general', 'explain', 'impact', 'onboard', 'entry', 'dependency')
REASONS = ('explicit_target', 'simple_orientation', 'impact', 'cross_module',
           'multi_step', 'default_agent', 'routing_fallback')


def safe_target(value: str | None) -> str | None:
    if value is None:
        return None
    from pycode.retrieval_evidence import safe_relative
    if (not isinstance(value, str) or not value or len(value) > 200
            or len(value.encode('utf-8')) > 768 or safe_relative(value) != value
            or any(ord(c) < 32 or ord(c) == 127 for c in value)):
        raise ValueError('Invalid question target')
    return value


@dataclass(frozen=True)
class StrategyDecision:
    strategy: str
    intent: str
    target: str | None
    reasons: tuple[str, ...]
    evidence_requirement: str
    policy_version: str = POLICY_VERSION


def _route(text: str, target: str | None) -> StrategyDecision:
    lower = text.casefold()
    if re.search(r'跨模块|跨文件|调用链|cross[- ](?:module|file)|call (?:chain|graph)', lower):
        return StrategyDecision('agent', 'general', target, ('cross_module',), 'multi_step')
    if re.search(r'排查|诊断|多步|逐步|debug|investigat|multi[- ]step|step.by.step', lower):
        return StrategyDecision('agent', 'general', target, ('multi_step',), 'multi_step')
    if target is None:
        match = re.search(r'(?<![\w/\\.:-])([\w.-]+(?:/[\w.-]+)*\.py)(?![\w/\\.])', text)
        if match:
            target = safe_target(match[1])
        else:
            symbol = re.search(r'`([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)`', text)
            if symbol:
                target = safe_target(symbol[1])
    if target is not None:
        intent = 'impact' if re.search(r'影响|修改|变更|impact|change|modif', lower) else (
            'dependency' if re.search(r'依赖|dependenc|imports?', lower) else
            'explain' if target.endswith('.py') else 'general')
        return StrategyDecision('fast', intent, target,
                                ('impact' if intent == 'impact' else 'explicit_target',), 'snapshot_evidence')
    for pattern, intent in ((r'入口|entry\b|entrypoint', 'entry'),
                            (r'入门|阅读顺序|概览|onboard|overview', 'onboard')):
        if re.search(pattern, lower):
            return StrategyDecision('fast', intent, None, ('simple_orientation',), 'snapshot_evidence')
    return StrategyDecision('agent', 'general', None, ('default_agent',), 'multi_step')


def route_question(text: str, target: str | None = None) -> StrategyDecision:
    if not isinstance(text, str) or not text.strip() or len(text) > 8000:
        raise ValueError('Invalid question')
    target = safe_target(target)
    try:
        return _route(text, target)
    except Exception:
        # This catches rule implementation failures only, never infrastructure,
        # authorization, request validation or retrieval failures.
        return StrategyDecision('agent', 'general', target, ('routing_fallback',), 'multi_step')


PREFLIGHT_REASONS = ('evidence_empty', 'source_unavailable', 'partial_coverage',
                     'budget_limited', 'relations_unconfirmed', 'preflight_failed')


def question_query(question, decision):
    from pycode.retrieval_evidence import Query, RetrievalRequest
    target = decision['target']
    text = question
    if target and decision['intent'] == 'general' and target not in text:
        text += '\n' + target
    return RetrievalRequest(Query(text, decision['intent'], target), 8 if decision['intent'] == 'impact' else 6)


def assess_evidence(result, context):
    """Evidence sufficiency, not a probability or model answer-quality score."""
    reasons = []
    if not result.items:
        reasons.append('evidence_empty')
    if result.status == 'unavailable':
        reasons.append('source_unavailable')
    if any(not item.snippet or item.coverage not in {'complete_symbol', 'complete_file'} for item in result.items):
        reasons.append('partial_coverage')
    if result.pack and any(name in {'token_budget', 'item_limit', 'file_diversity'} and count
                           for name, count in result.pack.reason_counts):
        reasons.append('budget_limited')
    if any(code in result.diagnostics for code in ('entry_unknown_reading_order_only', 'target_not_indexed')):
        reasons.append('relations_unconfirmed')
    if result.intent in {'impact', 'dependency'}:
        graph = context.graph
        paths = {item.path for item in result.items}
        if graph is None or graph.schema_version < 2 or any(
            site.get('path') in paths and site.get('resolution') in {'unknown', 'ambiguous'}
            for site in getattr(graph, 'call_sites', ())
        ):
            reasons.append('relations_unconfirmed')
    # A vector outage alone does not invalidate complete keyword/structural code.
    return tuple(dict.fromkeys(reasons))


def _pack_digest(result, target):
    data = dict(question=result.question, intent=result.intent, target=target,
        status=result.status, diagnostics=result.diagnostics, mode=result.mode,
        graph_schema_version=result.graph_schema_version, graph_stats=result.graph_stats,
        binding=result.binding.to_dict()
        if hasattr(result.binding, 'to_dict') else vars(result.binding),
        items=[item.to_dict() for item in result.items], pack=result.pack.summary() if result.pack else None)
    return hashlib.sha256(json.dumps(data, ensure_ascii=False, sort_keys=True, allow_nan=False).encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class InitialEvidence:
    """One call-scoped, context-bound Pack. Not accepted in any Tool/API schema."""
    context: object = field(repr=False, compare=False)
    result: object = field(repr=False, compare=False)
    target: str | None
    digest: str

    @classmethod
    def issue(cls, context, result, target):
        seed = cls(context, result, target, _pack_digest(result, target))
        seed.validate(context)
        return seed

    def validate(self, context):
        result = self.result
        if (context is not self.context or result.binding is None or result.binding != context.binding
            or result.pack is None or tuple(result.items) != result.pack.items
            or result.pack.budget != context.token_budget
            or result.pack.estimated_tokens > result.pack.budget
            or any((item.project_id, item.snapshot_id, item.commit_sha) !=
                (context.binding.project_id, context.binding.snapshot_id, context.binding.commit_sha) for item in result.items)
            or self.digest != _pack_digest(result, self.target)):
            raise ValueError('Initial evidence does not match its trusted retrieval context')
        return result

    def matches(self, question, target, intent):
        return (question == self.result.question and (not target or target.replace('\\', '/') == self.target)
                and (not intent or intent.lower() == self.result.intent))

    def projection(self):
        from pycode.evidence_pack import tool_items
        self.validate(self.context)
        return dict(origin='question_preflight', initial_pack_digest=self.digest,
            question=self.result.question, intent=self.result.intent, evidence=self.result.evidence,
            items=tool_items(self.result.items), retrieval=self.result.summary())


@dataclass(frozen=True)
class QuestionPreflight:
    initial_evidence: InitialEvidence
    reasons: tuple[str, ...]
    event: object

    @property
    def status(self): return 'insufficient' if self.reasons else 'sufficient'

    def summary(self):
        pack = self.initial_evidence.result.pack
        return dict(status=self.status, reasons=list(self.reasons), pack_digest=self.initial_evidence.digest,
            selected_count=len(pack.items), discarded_count=pack.discarded_count,
            budget=pack.budget, estimated_tokens=pack.estimated_tokens)


def prepare_question(question, decision, context):
    from dataclasses import replace
    from datetime import datetime, timezone
    from pycode.retriever import retrieve
    from pycode.agent.trace import TraceEvent
    retrieval = retrieve(question_query(question, decision), context)
    if retrieval.question != question:
        retrieval = replace(retrieval, question=question)
    seed = InitialEvidence.issue(context, retrieval, decision['target'])
    reasons = assess_evidence(retrieval, context)
    value = QuestionPreflight(seed, reasons, None)
    event = TraceEvent('ContextAssembled', datetime.now(timezone.utc), data={
        'included_section_names': ['evidence'], 'skipped_section_names': [],
        'retrieval': retrieval.summary(), 'question_preflight': value.summary()})
    return replace(value, event=event)
