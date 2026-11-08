"""A06 Memory admission/lifecycle policy; no IO, models or extra retrieval stack."""
from collections import Counter
from datetime import datetime, timedelta, timezone
import re

from pycode.agent.memory_record import digest


MAX_PROPOSALS = 8
MAX_EXTRACTED = 4
MAX_EXTRACT_BODY_CHARS = 2048
MAX_EXTRACT_TOTAL_BYTES = 8192
MAX_EXTRACTION_INPUT_CHARS = 24000
MAX_SELECTION_CANDIDATES = 24
MAX_NEAR_CHECKS = 32
MAX_BODY_BYTES = 16384
CATEGORIES = {'project_context', 'code_note', 'analysis_hint'}
STATES = {'active', 'conflict', 'invalidated', 'deleted'}
REASONS = {'created', 'conflicting_content', 'explicit_revision', 'explicit_invalidation', 'explicit_delete'}
PUBLIC_REASONS = {'expired','snapshot_mismatch','project_mismatch','inactive','conflict',
    'superseded','unknown_source','sensitive_content','capacity','temporary','invalid_proposal',
    'duplicate','near_duplicate_candidate','proposal_limit','input_limit','empty_answer',
    'input_sensitive','failed_run','model_failed','keyword_fallback','llm_selection','no_match',
    'storage_unavailable','candidate_limit','disabled'}

# Refuse recognizable credential/contact material; no secret-bearing diagnostics.
_SENSITIVE = re.compile(
    r'(?i)(?:\b(?:[a-z0-9]+_)*(?:password|passwd|api[_ -]?key|access[_ -]?token|token|secret|authorization|cookie)\b'
    r'["\']?\s*[=:：]\s*["\']?(?!\[redacted\]|<redacted>)[^\s"\']+)'
    r'|\bbearer\s+[A-Za-z0-9._~+/=-]{6,}|\bsk-[A-Za-z0-9_-]{12,}'
    r'|-----BEGIN [A-Z ]*PRIVATE KEY-----|\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+'
    r'|\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|(?<!\d)(?:\+86[- ]?)?1[3-9]\d{9}(?!\d)'
    r'|(?:密码|密钥|令牌|手机号|身份证号|邮箱)\s*[=:：]\s*\S+'
    r'|\b(?:postgres(?:ql)?(?:\+\w+)?|rediss?|https?)://[^\s/@:]+:[^\s/@]+@'
)


def sensitive(text):
    return bool(_SENSITIVE.search(text))


def timestamp(value):
    if not isinstance(value, str):
        raise ValueError('Invalid Memory timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Memory timestamp requires timezone')
    return parsed.astimezone(timezone.utc)


def iso(value):
    return value.astimezone(timezone.utc).isoformat()


def lifecycle_for(item, now):
    origin = item.provenance['origin']
    category = ('project_context' if origin == 'user_explicit' and item.type in
                {'project','workflow','preference'} else 'code_note' if origin == 'code_derived'
                else 'analysis_hint')
    scope = ('project' if category == 'project_context' and item.provenance['project_id'] else
             'snapshot' if item.provenance['snapshot_id'] else 'local')
    days = None if category == 'project_context' else 30 if category == 'code_note' else 7
    return dict(category=category, scope=scope, state='active',
        expires_at=iso(now + timedelta(days=days)) if days else None,
        target=digest(dict(type=item.type,title=item.title.strip().casefold(),
                           related_files=sorted(item.related_files))),
        revision=1, parents=[], reason='created')


def validate_lifecycle(value):
    if not isinstance(value, dict) or set(value) != {'category','scope','state','expires_at',
                                                   'target','revision','parents','reason'}:
        raise ValueError('Invalid Memory lifecycle')
    if (value['category'] not in CATEGORIES or value['scope'] not in {'project','snapshot','local'}
            or value['state'] not in STATES or value['reason'] not in REASONS
            or type(value['revision']) is not int or not 1 <= value['revision'] <= 256
            or not isinstance(value['target'],str) or not re.fullmatch('[0-9a-f]{64}',value['target'])
            or not isinstance(value['parents'],list) or len(value['parents'])>8
            or any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{64}',v) for v in value['parents'])):
        raise ValueError('Invalid Memory lifecycle fields')
    if value['expires_at'] is not None:
        timestamp(value['expires_at'])
    expected={'active':{'created','explicit_revision'},'conflict':{'conflicting_content'},
              'invalidated':{'explicit_invalidation'},'deleted':{'explicit_delete'}}
    if value['reason'] not in expected[value['state']]:
        raise ValueError('Invalid Memory state/reason')


def namespace(item):
    p, life = item.provenance or {}, item.lifecycle or {}
    return (p.get('origin'), life.get('scope'), p.get('project_id'),
            p.get('snapshot_id') if life.get('scope') == 'snapshot' else None,
            p.get('commit_sha') if life.get('scope') == 'snapshot' else None)


def scope_rejection(item, scope):
    p, life = item.provenance, item.lifecycle
    if not p or not life:
        return 'unknown_source'
    if scope is None:
        return None  # Explicit local operations remain confined to their root.
    if p['project_id'] != scope.binding.project_id:
        return 'project_mismatch'
    if life['scope'] == 'project' and life['category'] == 'project_context' and p['origin'] == 'user_explicit':
        return None
    if any(p[k] != getattr(scope.binding,k) for k in ('snapshot_id','commit_sha')):
        return 'snapshot_mismatch'
    return None


def content_key(item):
    # Preserve complete text, case and indentation. Never hash a prefix or a
    # case-folded code fragment as if it were the same assertion.
    return digest(dict(type=item.type,title=item.title,summary=item.summary,body=item.body,
                       tags=sorted(item.tags),related_files=sorted(item.related_files)))


def near_duplicate(a, b):
    left, right = set(re.findall(r'\w+',a.body.casefold())),set(re.findall(r'\w+',b.body.casefold()))
    return bool(left and right and len(left & right)/len(left | right) >= .85)


def text_for(item):
    return '\n'.join([item.id,item.title,item.summary,item.body,*item.tags,*item.related_files])


def validate_content(item):
    if not isinstance(item.body,str) or len(item.body.encode('utf-8')) > MAX_BODY_BYTES:
        raise ValueError('Memory body capacity exceeded')
    if sensitive(text_for(item)):
        raise ValueError('memory_sensitive_content')


def eligible(items, scope, now, counts=None):
    counts = counts if counts is not None else Counter()
    scoped = []
    for item in items:
        reason = scope_rejection(item,scope)
        if reason:
            counts[reason]+=1
        else:
            scoped.append(item)
    # Child records suppress parents even after expiry, preventing old facts
    # from returning when a revision expires. Different authority/scope cannot
    # suppress each other merely by naming a record id.
    blocked=set()
    by_id={item.record_id:item for item in scoped}
    for child in scoped:
        for record_id in child.lifecycle['parents']:
            parent=by_id.get(record_id)
            if parent is not None and namespace(child)==namespace(parent):
                blocked.add(parent.record_id)
    result=[]
    for item in scoped:
        life=item.lifecycle
        if item.record_id in blocked: reason='superseded'
        elif life['state']=='conflict': reason='conflict'
        elif life['state']!='active': reason='inactive'
        elif life['expires_at'] and timestamp(life['expires_at'])<=now: reason='expired'
        elif sensitive(text_for(item)): reason='sensitive_content'
        else:
            result.append(item)
            continue
        counts[reason]+=1
    return result


def extraction_gate(task, messages, answer):
    if not answer or not answer.strip(): return 'empty_answer'
    parts=[task,answer,*[getattr(m,'content','') for m in messages[-10:]]]
    if any(not isinstance(v,str) for v in parts): return 'invalid_proposal'
    if sum(map(len,parts))>MAX_EXTRACTION_INPUT_CHARS: return 'input_limit'
    if sensitive('\n'.join(parts)): return 'input_sensitive'
    return None


def public_summary(value):
    """Strict shared envelope for private Core -> live/persisted/replay/React."""
    fields={'schema_version','selected_count','created_count','reason_counts','source_counts','references'}
    if not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int or value['schema_version']!=1: return None
    if any(type(value[k]) is not int or not 0<=value[k]<=256 for k in ('selected_count','created_count')):return None
    for key,allowed in (('reason_counts',PUBLIC_REASONS),('source_counts',{'user_explicit','code_derived','model_inference','local_unknown'})):
        counts=value[key]
        if not isinstance(counts,dict) or any(k not in allowed or type(v)is not int or not 0<=v<=10000 for k,v in counts.items()):return None
    refs=value['references']
    if not isinstance(refs,list) or len(refs)>5:return None
    for ref in refs:
        if (not isinstance(ref,dict) or set(ref)!={'record_id','origin','reason'}
            or not isinstance(ref['record_id'],str) or not re.fullmatch('[0-9a-f]{64}',ref['record_id'])
            or ref['origin'] not in {'user_explicit','code_derived','model_inference','local_unknown'}
            or ref['reason'] not in {'keyword_fallback','llm_selection','local_search'}):return None
    return value
