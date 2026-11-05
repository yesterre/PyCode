"""Versioned, code-free Run metadata. No routing, models, or source selection."""
from copy import deepcopy
from dataclasses import replace
import json
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.core.errors import BackendError


LIMITS = {'request_metadata': 2048, 'execution_metadata': 2048, 'result_metadata': 16384}
DIAGNOSTICS = Literal['evidence_unavailable', 'evidence_empty', 'evidence_invalid',
    'evidence_binding_mismatch', 'evidence_omitted', 'chunk_ids_omitted',
    'retrieval_degraded', 'static_graph_only', 'execution_failed']


class StrictMetadata(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True, hide_input_in_errors=True)


class Binding(StrictMetadata):
    project_id: str
    snapshot_id: str
    commit_sha: str = Field(pattern=r'^[0-9a-f]{40,64}$')

    @field_validator('project_id', 'snapshot_id')
    @classmethod
    def uuid_value(cls, value):
        if str(UUID(value)) != value:
            raise ValueError('Noncanonical binding identity')
        return value


class Versioned(StrictMetadata):
    schema_version: Literal[1]
    binding: Binding

    @model_validator(mode='before')
    @classmethod
    def version(cls, value):
        if not isinstance(value, dict) or type(value.get('schema_version')) is not int:
            raise ValueError('Invalid metadata version')
        return value


class RequestMetadata(Versioned):
    entrypoint: Literal['ask', 'impact', 'agent']


class ExecutionMetadata(Versioned):
    strategy: Literal['fast', 'agent']
    source: Literal['legacy_entrypoint']


class QuestionRequestMetadata(Versioned):
    schema_version: Literal[2]
    entrypoint: Literal['question']
    target: str | None = None

    @field_validator('target')
    @classmethod
    def target_value(cls, value):
        from pycode.question import safe_target
        return safe_target(value)


class QuestionExecutionMetadata(QuestionRequestMetadata):
    strategy: Literal['fast', 'agent']
    source: Literal['rules']
    policy_version: Literal['a04b-v1']
    intent: Literal['general', 'explain', 'impact', 'onboard', 'entry', 'dependency']
    reasons: list[Literal['explicit_target', 'simple_orientation', 'impact', 'cross_module',
                          'multi_step', 'default_agent', 'routing_fallback']] = Field(min_length=1, max_length=4)
    evidence_requirement: Literal['snapshot_evidence', 'multi_step']

    @model_validator(mode='after')
    def consistent_decision(self):
        if ((self.strategy == 'agent') != (self.evidence_requirement == 'multi_step')
                or len(set(self.reasons)) != len(self.reasons)):
            raise ValueError('Inconsistent question decision')
        return self


class EvidenceReference(StrictMetadata):
    path: str = Field(min_length=1, max_length=500)
    symbol: str | None = Field(default=None, max_length=200)
    start_line: int | None
    end_line: int | None
    chunk_ids: list[str] = Field(max_length=4)
    chunk_ids_omitted: int = Field(ge=0)
    source: Literal['exact_path', 'exact_symbol', 'keyword', 'graph', 'entry', 'vector', 'hybrid']
    coverage: Literal['complete_symbol', 'complete_file', 'unavailable']
    relation_resolution: Literal['static', 'declaration', 'legacy', 'unknown'] | None = None

    @model_validator(mode='after')
    def safe_reference(self):
        from pycode.retrieval_evidence import safe_relative
        if safe_relative(self.path) != self.path or any(ord(c) < 32 or ord(c) == 127 for c in self.path):
            raise ValueError('Unsafe evidence path')
        if self.symbol is not None and any(ord(c) < 32 or ord(c) == 127 for c in self.symbol):
            raise ValueError('Unsafe symbol')
        if (self.start_line is not None or self.end_line is not None) and (
            self.start_line is None or self.end_line is None or not 1 <= self.start_line <= self.end_line):
            raise ValueError('Invalid evidence range')
        if self.coverage!='unavailable' and self.start_line is None:
            raise ValueError('Complete evidence requires a source range')
        import re
        if any(not re.fullmatch(r'[0-9a-f]{64}', value) for value in self.chunk_ids):
            raise ValueError('Invalid Chunk identity')
        return self


class ResultMetadata(Versioned):
    outcome: Literal['completed', 'failed']
    quality: Literal['evidence_available', 'partial', 'insufficient', 'unavailable']
    intent: Literal['general', 'explain', 'impact', 'onboard', 'entry', 'dependency'] | None
    references: list[EvidenceReference] = Field(max_length=8)
    references_omitted: int = Field(ge=0)
    diagnostics: list[DIAGNOSTICS] = Field(max_length=16)

    @model_validator(mode='after')
    def consistent_quality(self):
        if (len(set(self.diagnostics))!=len(self.diagnostics)
            or (not self.references and self.quality not in {'insufficient','unavailable'})
            or (self.outcome=='failed' and self.quality not in {'partial','unavailable'})
            or ((self.outcome=='failed') != ('execution_failed' in self.diagnostics))
            or (self.references_omitted>0 and 'evidence_omitted' not in self.diagnostics)
            or (any(ref.chunk_ids_omitted for ref in self.references) and 'chunk_ids_omitted' not in self.diagnostics)
            or (self.quality=='evidence_available' and (self.diagnostics or any(ref.coverage=='unavailable' for ref in self.references)))):
            raise ValueError('Inconsistent result evidence quality')
        return self


MODELS = dict(request_metadata=RequestMetadata, execution_metadata=ExecutionMetadata,
              result_metadata=ResultMetadata)


def invalid_metadata():
    return BackendError('invalid_run_metadata', 'Run metadata is invalid or unsupported.')


def validated(name, value):
    if value is None:
        return None
    try:
        # Bound hostile containers before validation; never echo any input.
        if len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')) > LIMITS[name]:
            raise ValueError()
        model = MODELS[name]
        if isinstance(value, dict) and type(value.get('schema_version')) is int and value['schema_version'] == 2:
            model = {'request_metadata': QuestionRequestMetadata,
                     'execution_metadata': QuestionExecutionMetadata}.get(name, model)
        return model.model_validate(value).model_dump(mode='json')
    except (ValueError, TypeError, KeyError, RecursionError):
        raise invalid_metadata() from None


def binding_for(snapshot):
    return dict(project_id=str(snapshot.project_id), snapshot_id=str(snapshot.id), commit_sha=snapshot.commit_sha)


def legacy_request(snapshot, entrypoint):
    return validated('request_metadata', dict(schema_version=1, binding=binding_for(snapshot), entrypoint=entrypoint))


def legacy_execution(request):
    if request is None:
        return None
    return dict(schema_version=1, binding=deepcopy(request['binding']),
                strategy='agent' if request['entrypoint']=='agent' else 'fast', source='legacy_entrypoint')


def question_metadata(snapshot, decision, target):
    request = dict(schema_version=2, binding=binding_for(snapshot), entrypoint='question', target=target)
    execution = dict(request, strategy=decision.strategy, source='rules', policy_version=decision.policy_version,
                     intent=decision.intent, target=decision.target, reasons=list(decision.reasons),
                     evidence_requirement=decision.evidence_requirement)
    return validated('request_metadata', request), validated('execution_metadata', execution)


def checked(project_id, snapshot_id, run_type, status, snapshot, request, execution, result):
    values = {name: validated(name, value) for name, value in zip(MODELS, (request, execution, result))}
    if not any(value is not None for value in values.values()):
        if run_type == 'question':
            raise invalid_metadata()
        return values  # Historical rows remain genuinely empty.
    request, execution, result = values.values()
    if (request is None or snapshot is None or str(snapshot.project_id)!=str(project_id)
        or str(snapshot.id)!=str(snapshot_id) or request['entrypoint']!=run_type
        or any(value is not None and value['binding']!=binding_for(snapshot) for value in values.values())
        or (status!='queued' and execution is None)
        or (request['schema_version']==1 and execution is not None and execution!=legacy_execution(request))
        or (request['schema_version']==2 and (execution is None or execution['schema_version']!=2
            or (request['target'] is not None and request['target']!=execution['target'])))
        or ((status in {'completed','failed'}) != (result is not None))
        or (result is not None and result['outcome']!=status)):
        raise invalid_metadata()
    return values


def public_run(run, snapshot):
    """Authorize first. Unsupported stored versions remain in SQL, never leak."""
    try:
        values=checked(run.project_id,run.snapshot_id,run.run_type,run.status,snapshot,
                       run.request_metadata,run.execution_metadata,run.result_metadata)
        return replace(run, **values, metadata_warnings=())
    except BackendError:
        return replace(run, request_metadata=None, execution_metadata=None, result_metadata=None,
                       metadata_warnings=('run_metadata_unavailable',))


def require_quarantine(run,snapshot):
    """Only corrupt/unsupported in-flight records may enter this failure path.

    Preserve their original text and metadata rather than manufacturing a valid
    binding or erasing data. Normal state/metadata transitions remain strict.
    """
    if run.status not in {'queued','running'}:
        raise BackendError('agent_run_state_conflict','Agent run cannot make the requested state transition.')
    try:
        checked(run.project_id,run.snapshot_id,run.run_type,run.status,snapshot,
                run.request_metadata,run.execution_metadata,run.result_metadata)
    except BackendError as exc:
        if exc.code=='invalid_run_metadata':return
        raise
    raise BackendError('agent_run_state_conflict','Valid Run metadata cannot be quarantined.')


def result_for(run, retrievals=(), *, intent=None, failed=False):
    """Project structured summaries only. Never parse result/evidence strings."""
    if run.request_metadata is None:
        return None
    from backend.app.core.application_trace import public_retrieval_summary
    binding=validated('request_metadata',run.request_metadata)['binding']
    references=[];diagnostics=[];omitted=0;observed=False;seen=set()
    for raw in retrievals:
        if raw is None:
            continue
        try:value=public_retrieval_summary(raw)
        except (ValueError,TypeError,KeyError,AttributeError,OverflowError):value=None
        if value is None:
            diagnostics.append('evidence_invalid');continue
        if value['binding']!=binding:
            diagnostics.append('evidence_binding_mismatch');continue
        observed=True
        if value['status']!='ok':diagnostics.append('retrieval_degraded')
        omitted+=value['references_omitted']
        for index,ref in enumerate(value['references']):
            original=raw['references'][index]
            if original.get('path')!=ref['path'] or original.get('symbol')!=ref['symbol']:
                # A redacted or abbreviated address cannot masquerade as an exact one.
                omitted+=1;continue
            selected={k:ref[k] for k in ('path','symbol','start_line','end_line','chunk_ids','chunk_ids_omitted','source','coverage')}
            selected['relation_resolution']=ref.get('relation_resolution')
            try:selected=EvidenceReference.model_validate(selected).model_dump(mode='json')
            except (ValueError,TypeError):diagnostics.append('evidence_invalid');continue
            key=json.dumps(selected,sort_keys=True)
            if key in seen:continue
            seen.add(key)
            if len(references)<8:references.append(selected)
            else:omitted+=1
            if selected['chunk_ids_omitted']:diagnostics.append('chunk_ids_omitted')
            if 'graph_distance' in ref or ref['source']=='graph':diagnostics.append('static_graph_only')
    if omitted:diagnostics.append('evidence_omitted')
    if not references:diagnostics.append('evidence_empty' if observed else 'evidence_unavailable')
    if failed:diagnostics.append('execution_failed')
    diagnostics=list(dict.fromkeys(diagnostics))
    partial=bool(diagnostics) or any(ref['coverage']=='unavailable' for ref in references)
    quality=('partial' if partial else 'evidence_available') if references else ('insufficient' if observed and not failed else 'unavailable')
    metadata=dict(schema_version=1,binding=deepcopy(binding),outcome='failed' if failed else 'completed',
        quality=quality,intent=intent if intent in {'general','explain','impact','onboard','entry','dependency'} else None,
        references=references,references_omitted=omitted,diagnostics=diagnostics)
    # Unicode and JSON escaping can exceed a byte budget even with field caps.
    # Omit whole reference records explicitly; never silently crop an address.
    while len(json.dumps(metadata,ensure_ascii=False).encode('utf-8'))>LIMITS['result_metadata']:
        metadata['references'].pop();metadata['references_omitted']+=1
        metadata['quality']='partial' if metadata['references'] else 'unavailable'
        if 'evidence_omitted' not in metadata['diagnostics']:metadata['diagnostics'].append('evidence_omitted')
    return validated('result_metadata',metadata)


def agent_retrievals(result):
    if getattr(result, 'retrieval', None) is not None:
        return [result.retrieval.summary()]
    return [getattr(tool,'data',{}).get('retrieval') for tool in getattr(result,'tool_results',())
            if isinstance(getattr(tool,'data',None),dict)]
