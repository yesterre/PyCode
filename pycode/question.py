"""Bounded, deterministic rules for the unified entrance; no IO or model calls."""
from dataclasses import dataclass
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
