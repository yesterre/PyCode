"""Atomic context selection for the existing assembler and its actual renderers."""
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass, field
import hashlib
import json
import re


ESTIMATOR = 'utf8_bytes_upper_bound_v1'
REASONS = {'context_budget','tool_output_limit','duplicate','invalid_evidence','auxiliary_context'}
SOURCES = {'snapshot_source','static_graph','memory_hint','tool_observation','unbound_source'}
WARNINGS = {'reference_invalid','binding_mismatch','definition_missing','source_conflict',
            'relation_unknown','unbound_source','memory_hint_only','context_omitted',
            'citation_missing','citation_invalid','partial_coverage'}


def encoded(value):
    return json.dumps(value,ensure_ascii=False,indent=2,default=str)


def digest(value):
    return hashlib.sha256(encoded(value).encode('utf-8')).hexdigest()


def estimate(prompt):
    # Includes a conservative reserve for the single-message transport envelope.
    return len(prompt.encode('utf-8')) + 512


@dataclass
class ContextUnit:
    content: str
    reference: dict = field(default_factory=dict)
    important: bool = False
    id: str = ''

    def __post_init__(self):
        if not self.id:
            self.id = digest([self.reference,self.content])


def limits():
    from pycode.agent.budget import current_budget, ExecutionLimits
    budget=current_budget()
    return budget.limits if budget is not None else ExecutionLimits()


def make_report(phase, prompt, selected, discarded, *, pack_digests=()):
    cap=limits()
    from pycode.agent.budget import current_budget
    active=current_budget()
    binding=asdict(active.binding) if active is not None and active.binding is not None else None
    ranked=sorted(selected,key=lambda unit:(not unit.important,
        unit.reference.get('source') not in {'snapshot_source','static_graph'},-(unit.reference.get('turn') or 0)))
    refs=[dict(unit.reference,id=unit.id) for unit in ranked if unit.reference]
    decisions=[dict(id=unit.id,reason=reason,reference=unit.reference) for unit,reason in discarded]
    value=dict(schema_version=1,binding=binding,phase=phase,delivered_to_model=False,budget=cap.context_tokens,estimated_tokens=estimate(prompt),
        estimator=ESTIMATOR,tool_output_chars=cap.tool_output_chars,selected_count=len(selected),
        discarded_count=len(discarded),reason_counts=dict(sorted(Counter(reason for _,reason in discarded).items())),
        references=refs[:8],references_omitted=max(0,len(refs)-8),
        decisions=decisions[:8],decisions_omitted=max(0,len(decisions)-8),
        pack_digests=list(dict.fromkeys(pack_digests))[:4])
    # Whole references only, never crop a citation address to fit a public record.
    while len(encoded(value).encode('utf-8'))>6144 and value['references']:
        value['references'].pop();value['references_omitted']+=1
    while len(encoded(value).encode('utf-8'))>6144 and value['decisions']:
        value['decisions'].pop();value['decisions_omitted']+=1
    return value


def remember(report, prompt):
    from pycode.agent.budget import current_budget
    if (budget:=current_budget()) is not None:
        budget.context_reports.append(deepcopy(report))
        budget.prompt_digest=digest(prompt)
        if report['phase'] in {'next_action','summary'}:
            budget.answer_context=deepcopy(report)
        if budget.context_observer is not None:
            budget.context_observer(report)


def refuse(report, prompt, reason='budget_context'):
    remember(report,prompt)
    from pycode.agent.budget import current_budget
    if (budget:=current_budget()) is not None:
        budget.stop(reason)
    raise ValueError('Minimum context exceeds the configured budget')


def fit_context(context, renderer, *, phase='summary'):
    """Fit whole units/sections, measuring the COMPLETE rendered model input."""
    cap=limits();discarded=list(context.discarded_units)
    selected={s.name:list(s.units) for s in context.sections if s.units}
    removed=set()
    def refresh():
        for section in context.sections:
            if section.name in selected:
                section.content='\n\n'.join(u.content for u in selected[section.name])
        visible=[u for s in context.sections for u in selected.get(s.name,[])]
        context.selection_notice=dict(selected_ids=[u.id for u in visible[:8]],
            selected_ids_omitted=max(0,len(visible)-8),discarded_count=len(discarded),
            reason_counts=dict(sorted(Counter(r for _,r in discarded).items())),
            partial=bool(discarded),warnings=context.verification_warnings)
        return renderer(context)
    prompt=refresh()
    while estimate(prompt)>cap.context_tokens:
        # Lower priority auxiliary state first. Preserve source evidence and recent
        # observations until metadata/Memory/old observations have been considered.
        candidates=[]
        for section in context.sections:
            if section.name in {'identity','policy','project','response_contract','tools'}:
                continue
            if section.name in selected and selected[section.name]:
                for index,unit in enumerate(selected[section.name]):
                    weight=0 if section.name in {'memory','memory_index','trace','tasks','evidence'} else 1
                    if unit.important:weight=3
                    candidates.append((weight,index,section,unit))
            elif section.name not in selected and section.name not in removed:
                candidates.append((0 if section.name in {'memory_index','trace','tasks','evidence'} else 1,
                                   0,section,None))
        if not candidates:
            units=[u for values in selected.values() for u in values]
            context.report=make_report(phase,prompt,units,discarded,pack_digests=context.pack_digests)
            refuse(context.report,prompt)
        _,_,section,unit=min(candidates,key=lambda row:(row[0],row[1],-row[2].priority))
        if unit is not None:
            selected[section.name].remove(unit)
            discarded.append((unit,'context_budget'))
            if not selected[section.name]:
                removed.add(section.name)
        else:
            removed.add(section.name)
            discarded.append((ContextUnit(section.content,{'source':'memory_hint' if section.name.startswith('memory') else 'tool_observation'}),'context_budget'))
        if section.name in removed:
            context.sections.remove(section)
            section.included=False;section.reason='Omitted whole section: context_budget'
            section.content='';section.units=[]
            context.skipped_sections.append(section)
        prompt=refresh()
    units=[u for section in context.sections for u in selected.get(section.name,[])]
    context.report=make_report(phase,prompt,units,discarded,pack_digests=context.pack_digests)
    context.discarded_units=discarded
    if discarded and 'context_omitted' not in context.verification_warnings:
        context.verification_warnings.append('context_omitted')
    remember(context.report,prompt)
    from pycode.agent.budget import current_budget
    if (budget:=current_budget()) is not None:
        budget.verification_warnings.update(context.verification_warnings)
        budget.evidence_rejected |= context.rejected
        budget.answer_references=[deepcopy(u.reference) for u in units]
    if context.rejected:
        refuse(context.report,prompt,'evidence_invalid')
    return prompt


def fit_planner(payload,renderer,*,trusted=None):
    """Initial Planner uses the same atomic units and exact render measurement."""
    payload=deepcopy(payload);cap=limits();discarded=[];selected=[];pack_digests=[]
    seed=payload.get('initial_evidence')
    if seed:
        from pycode.agent.evidence import inspect_context_evidence
        issues,bad=inspect_context_evidence('retrieve_context',seed,trusted)
        from pycode.agent.budget import current_budget
        if (budget:=current_budget()) is not None:
            budget.verification_warnings.update(issues);budget.evidence_rejected|=bad
        if bad:
            seed['items']=[]
            refuse(make_report('plan',renderer(payload),[],[]),renderer(payload),'evidence_invalid')
        pack_digests.append(seed['initial_pack_digest'])
        units=[ContextUnit(encoded(item),reference_for(item),item.get('source') in {'exact_path','exact_symbol'})
               for item in seed.get('items',[])]
        seed['context_selection']={'origin_pack_digest':seed['initial_pack_digest'],'selected_ids':[u.id for u in units],
                                   'discarded_count':0,'partial':False}
    else:units=[]
    seed_ids={u.id for u in units};remaining=cap.tool_output_chars
    for unit in list(units):
        if len(unit.content)>remaining:
            units.remove(unit);discarded.append((unit,'tool_output_limit'))
        else:remaining-=len(unit.content)
    def render():
        if seed:
            # Keep original preflight metadata labelled as such; it does not describe
            # this smaller projection. A04's immutable Pack and digest stay intact.
            seed['items']=[json.loads(u.content) for u in units]
            seed['evidence']=list(dict.fromkeys(i.get('path') for i in seed['items'] if i.get('path')))
            seed['context_selection'].update(selected_ids=[u.id for u in units],
                discarded_count=sum(u.id in seed_ids for u,_ in discarded),
                partial=any(u.id in seed_ids for u,_ in discarded))
            seed['retrieval_scope']='original_pack_not_prompt_selection'
        return renderer(payload)
    prompt=render()
    while estimate(prompt)>cap.context_tokens:
        if payload.get('relevant_memories'):
            value=payload['relevant_memories'].pop(0)
            discarded.append((ContextUnit(encoded(value),{'source':'memory_hint'}),'context_budget'))
        elif payload.get('memory_index'):
            discarded.append((ContextUnit(payload['memory_index'],{'source':'memory_hint'}),'context_budget'))
            payload['memory_index']=''
        elif units:
            unit=next((u for u in reversed(units) if not u.important),units[-1])
            units.remove(unit);discarded.append((unit,'context_budget'))
        elif payload.get('project_summary'):
            discarded.append((ContextUnit(encoded(payload['project_summary'])),'context_budget'))
            payload['project_summary']={}
        else:
            report=make_report('plan',prompt,[],discarded,pack_digests=pack_digests)
            refuse(report,prompt)
        prompt=render()
    selected.extend(units)
    report=make_report('plan',prompt,selected,discarded,pack_digests=pack_digests)
    remember(report,prompt)
    return prompt


def reference_for(item, *, tool='retrieve_context', turn=None):
    from pycode.retrieval_evidence import safe_relative
    path=item.get('path')
    if path is not None:
        path=safe_relative(path)
        if len(path)>500 or any(ord(c)<32 or ord(c)==127 for c in path):raise ValueError('Invalid evidence path')
    symbol=item.get('symbol')
    if symbol is not None and len(symbol)>200:symbol=None
    chunks=list(item.get('chunk_ids',()))
    return dict(source='static_graph' if item.get('source')=='graph' else
                'snapshot_source' if item.get('snapshot_id') else 'unbound_source',
        tool=tool,turn=turn,path=path,symbol=symbol,start_line=item.get('start_line'),end_line=item.get('end_line'),
        chunk_ids=chunks[:4],chunk_ids_omitted=max(0,len(chunks)-4),coverage=item.get('coverage','unavailable'),
        content_hash=digest(item.get('snippet',item.get('content',''))))


def result_units(steps, results, turns=(), *, trusted=None):
    """Every observed result participates; plan association is never positional."""
    from pycode.agent.evidence import inspect_context_evidence
    units=[];discarded=[];warnings=[];rejected=False
    by_todo={s.todo_id:s for s in steps if s.todo_id is not None}
    observed={id(turn.tool_result):turn for turn in turns if turn.tool_result is not None}
    for index,result in enumerate(results,1):
        turn=observed.get(id(result));call=turn.tool_call if turn else None
        step=by_todo.get(call.planned_todo_id) if call else None
        data=deepcopy(result.data)
        try:issues,bad=inspect_context_evidence(result.tool,data,trusted)
        except (TypeError,ValueError,KeyError,AttributeError):issues,bad=['reference_invalid'],True
        warnings.extend(issues);rejected|=bad
        if bad:
            discarded.append((ContextUnit('',dict(source='tool_observation',tool=result.tool,
                turn=turn.index if turn else index),id=digest([index,result.tool,data])),'invalid_evidence'))
            continue
        heading='\n'.join([f'## Step {index}: {result.tool}',f'Purpose: {step.reason if step else "Unbound observation"}',
            f'Required: {step.required if step else False}',f'Todo: {step.todo_id if step else "unbound"}',
            f'Status: {"ok" if result.ok else "failed"}',f'Summary: {result.safe_summary}',
            f'Error: {result.safe_error or "N/A"}',
            f'Execution: {encoded(result.execution.to_dict() if result.execution else None)}'])
        parts=[]
        if result.tool=='retrieve_context' and isinstance(data.get('items'),list):
            for item in data['items']:
                if not isinstance(item,dict):
                    warnings.append('reference_invalid');rejected=True;continue
                try:ref=reference_for(item,turn=turn.index if turn else index)
                except (ValueError,TypeError):
                    warnings.append('reference_invalid');rejected=True;continue
                parts.append(({'item':item,'origin_pack_digest':data.get('initial_pack_digest'),
                    'coverage':item.get('coverage'),'source_class':ref['source']},ref,item.get('source') in {'exact_path','exact_symbol'}))
        elif result.tool=='read_file' and data.get('path'):
            start=data.get('start_line',1)
            actual_end=start+max(0,len(data.get('content','').split('\n'))-1)
            original_end=data.get('end_line') or data.get('line_count')
            partial=data.get('truncated') or start!=1 or original_end!=data.get('line_count')
            item=dict(data, snapshot_id=trusted.binding.snapshot_id if trusted and trusted.binding else None,
                end_line=actual_end,coverage='partial' if partial else 'complete_file',
                chunk_ids=[c.id for c in (trusted.chunks.chunks if trusted and trusted.chunks else [])
                           if c.path==data['path'] and c.span.start_line<=actual_end and c.span.end_line>=start])
            if not data.get('content'):
                item.update(start_line=None,end_line=None,coverage='unavailable')
            try:ref=reference_for(item,tool=result.tool,turn=turn.index if turn else index)
            except (ValueError,TypeError):ref={'source':'unbound_source'};bad=True;rejected=True
            if trusted is not None and data['path'] not in dict(trusted.sources):
                ref.update(source='tool_observation',coverage='unavailable')
                item['coverage']='unavailable'
            parts.append((dict(data,source_class=ref['source'],coverage=item['coverage'],
                requested_end_line=original_end,end_line=actual_end),ref,True))
        elif result.tool=='query_graph' and data.get('nodes'):
            for node in data['nodes']:
                item=dict(path=node.get('path'),symbol=node.get('name'),start_line=node.get('start_line'),
                    end_line=node.get('end_line'),source='graph',coverage='unavailable',chunk_ids=[])
                try:ref=reference_for(item,tool=result.tool,turn=turn.index if turn else index)
                except (ValueError,TypeError):rejected=True;bad=True;continue
                parts.append((dict(node=node,edges=[edge for edge in data.get('edges',[])
                    if node['id'] in {edge.get('source'),edge.get('target')}],
                    call_sites=[site for site in data.get('call_sites',[]) if site.get('caller_id')==node['id']],
                    relation_status='static_only_not_runtime_verified'),ref,False))
        else:
            ref=dict(source='static_graph' if result.tool=='query_graph' else 'memory_hint'
                     if result.tool.startswith('memory') else 'tool_observation',tool=result.tool,
                     turn=turn.index if turn else index)
            parts.append((data,ref,False))
        if not parts:parts=[({'empty':True},{'source':'tool_observation','tool':result.tool},False)]
        remaining=limits().tool_output_chars
        for data_part,ref,important in parts:
            content=heading+'\nSource class: '+ref['source']+'\nData:\n'+encoded(data_part)
            unit=ContextUnit(content,ref,important)
            if bad:discarded.append((unit,'invalid_evidence'))
            elif len(content)>remaining:discarded.append((unit,'tool_output_limit'))
            else:units.append(unit);remaining-=len(content)
    # Keep the most recent equivalent code observation. IDs retain exact provenance.
    seen=set();selected=[]
    for unit in reversed(units):
        ref=unit.reference
        key=digest({k:ref.get(k) for k in ('source','path','start_line','end_line','content_hash')}) if ref.get('content_hash') else unit.id
        if key in seen:discarded.append((unit,'duplicate'))
        else:seen.add(key);selected.append(unit)
    return list(reversed(selected)),discarded,list(dict.fromkeys(warnings)),rejected


def public_context_report(value):
    """Whitelist an immutable code-free report for DB/API/Trace consumers."""
    fields={'schema_version','binding','phase','delivered_to_model','budget','estimated_tokens','estimator','tool_output_chars',
            'selected_count','discarded_count','reason_counts','references','references_omitted',
            'decisions','decisions_omitted','pack_digests'}
    try:
        if (not isinstance(value,dict) or set(value)!=fields or type(value['schema_version']) is not int
            or value['schema_version']!=1 or value['phase'] not in {'plan','next_action','summary','auxiliary'}
            or type(value['delivered_to_model']) is not bool
            or value['estimator']!=ESTIMATOR or len(encoded(value).encode('utf-8'))>8192):return None
        if value['binding'] is not None:
            from uuid import UUID
            binding=value['binding']
            if (not isinstance(binding,dict) or set(binding)!={'project_id','snapshot_id','commit_sha'}
                or str(UUID(binding['project_id']))!=binding['project_id']
                or str(UUID(binding['snapshot_id']))!=binding['snapshot_id']
                or not re.fullmatch(r'[0-9a-f]{40,64}',binding['commit_sha'])):return None
        bounds={'budget':(4096,1048576),'tool_output_chars':(256,1048576),'estimated_tokens':(0,10**12),
                'selected_count':(0,1000000),'discarded_count':(0,1000000),
                'references_omitted':(0,1000000),'decisions_omitted':(0,1000000)}
        if any(type(value[k]) is not int or not low<=value[k]<=high for k,(low,high) in bounds.items()):return None
        if value['delivered_to_model'] and value['estimated_tokens']>value['budget']:return None
        counts=value['reason_counts']
        if (not isinstance(counts,dict) or set(counts)-REASONS
            or any(type(n) is not int or n<0 for n in counts.values())
            or sum(counts.values())!=value['discarded_count']):return None
        refs=value['references'];decisions=value['decisions']
        if not isinstance(refs,list) or len(refs)>8 or not isinstance(decisions,list) or len(decisions)>8:return None
        if len(decisions)+value['decisions_omitted']!=value['discarded_count']:return None
        if len(refs)+value['references_omitted']>value['selected_count']:return None
        hex64=lambda s:isinstance(s,str) and re.fullmatch(r'[0-9a-f]{64}',s) is not None
        for decision in decisions:
            if (set(decision)!={'id','reason','reference'} or not hex64(decision['id'])
                or decision['reason'] not in REASONS or not isinstance(decision['reference'],dict)):return None
        allowed={'id','source','tool','turn','path','symbol','start_line','end_line','chunk_ids',
                 'chunk_ids_omitted','coverage','content_hash'}
        for ref in refs+[dict(d['reference'],id=d['id']) for d in decisions if d['reference']]:
            if not isinstance(ref,dict) or set(ref)-allowed or not hex64(ref['id']) or ref['source'] not in SOURCES:return None
            if ref.get('tool') is not None and not re.fullmatch(r'[A-Za-z_][\w.-]{0,63}',ref['tool']):return None
            if ref.get('turn') is not None and (type(ref['turn']) is not int or not 1<=ref['turn']<=128):return None
            path=ref.get('path')
            if path is not None:
                from pycode.retrieval_evidence import safe_relative
                if safe_relative(path)!=path or len(path)>500 or any(ord(c)<32 or ord(c)==127 for c in path):return None
            symbol=ref.get('symbol')
            if symbol is not None and (not isinstance(symbol,str) or len(symbol)>200 or any(ord(c)<32 for c in symbol)):return None
            start,end=ref.get('start_line'),ref.get('end_line')
            if (start is not None or end is not None) and (type(start) is not int or type(end) is not int or not 1<=start<=end):return None
            if 'content_hash' in ref and not hex64(ref['content_hash']):return None
            if 'coverage' in ref and ref['coverage'] not in {'complete_file','complete_symbol','partial','unavailable'}:return None
            if 'chunk_ids' in ref and (not isinstance(ref['chunk_ids'],list) or len(ref['chunk_ids'])>4
                                       or any(not hex64(c) for c in ref['chunk_ids'])):return None
            if 'chunk_ids_omitted' in ref and (type(ref['chunk_ids_omitted']) is not int or ref['chunk_ids_omitted']<0):return None
        if not isinstance(value['pack_digests'],list) or len(value['pack_digests'])>4 or any(not hex64(s) for s in value['pack_digests']):return None
        return deepcopy(value)
    except (ValueError,KeyError,TypeError,AttributeError,OverflowError):return None


def public_verification(value):
    if (not isinstance(value,dict) or set(value)!={'schema_version','status','warnings','natural_language_verified'}
        or type(value['schema_version']) is not int or value['schema_version']!=1
        or not isinstance(value['status'],str) or value['status'] not in {'checked','limited','unavailable','rejected'}
        or value['natural_language_verified'] is not False or not isinstance(value['warnings'],list)
        or len(value['warnings'])>len(WARNINGS) or any(not isinstance(s,str) or s not in WARNINGS for s in value['warnings'])
        or len(set(value['warnings']))!=len(value['warnings'])):return None
    return deepcopy(value)
