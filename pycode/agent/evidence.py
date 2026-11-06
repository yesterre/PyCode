from __future__ import annotations

from pycode.agent.types import AgentResult
from pycode.constants import DEFAULT_MEMORY_DIR
from pycode.utils import dedupe_preserve_order


def inspect_context_evidence(tool, data, trusted):
    """Validate explicit static evidence only; never infer natural-language truth."""
    from dataclasses import asdict
    from pycode.retrieval_evidence import safe_relative, source_lines
    warnings=[]; rejected=False
    if trusted is None or trusted.binding is None:
        return ['unbound_source'], False
    binding=asdict(trusted.binding)
    stated=data.get('binding') or data.get('retrieval',{}).get('binding') or data.get('graph',{}).get('binding')
    if stated is not None and stated!=binding:
        return ['binding_mismatch'],True
    sources=dict(trusted.sources)
    items=data.get('items',[]) if tool=='retrieve_context' else []
    for item in items:
        if any(item.get(k)!=v for k,v in binding.items()):
            warnings.append('binding_mismatch');rejected=True;continue
        try:
            path=safe_relative(item['path']);start,end=item.get('start_line'),item.get('end_line')
            if item.get('coverage')=='unavailable':
                if item.get('snippet'):
                    warnings.append('source_conflict');rejected=True
                warnings.append('definition_missing');continue
            lines=source_lines(sources[path])
            if type(start) is not int or type(end) is not int or not 1<=start<=end<=len(lines):
                raise ValueError()
            expected='\n'.join(f'{n}: {lines[n-1]}' for n in range(start,end+1))
            if item.get('snippet')!=expected:
                warnings.append('source_conflict');rejected=True
            chunks=[c for c in (trusted.chunks.chunks if trusted.chunks else [])
                    if c.path==path and c.span.start_line<=end and c.span.end_line>=start]
            if set(item.get('chunk_ids',()))!={c.id for c in chunks}:
                raise ValueError()
            symbol=item.get('symbol_key')
            if symbol and not any(c.span.symbol_key==symbol for c in chunks):
                warnings.append('definition_missing');rejected=True
            if item.get('coverage')=='complete_symbol' and not any(
                c.span.qualified_name==item.get('symbol') and c.span.symbol_start_line==start
                and c.span.symbol_end_line==end for c in chunks):
                warnings.append('definition_missing');rejected=True
        except (ValueError,KeyError,TypeError):
            warnings.append('reference_invalid');rejected=True
        if item.get('graph_path') or item.get('source')=='graph':warnings.append('relation_unknown')
    if tool=='read_file' and data.get('path'):
        try:
            path=safe_relative(data['path']);start=data.get('start_line',1)
            if path not in sources:
                return list(dict.fromkeys([*warnings,'unbound_source'])),False
            lines=source_lines(sources[path]);end=data.get('end_line') or len(lines)
            if not lines and data.get('content')=='':return ['definition_missing'],False
            if type(start) is not int or type(end) is not int or not 1<=start<=end<=len(lines):raise ValueError()
            expected='\n'.join(lines[start-1:end])
            if data.get('truncated'):
                warnings.append('partial_coverage')
                if not expected.startswith(data.get('content','')):raise ValueError()
            elif data.get('content')!=expected:raise ValueError()
            if start!=1 or end!=len(lines):warnings.append('partial_coverage')
        except (ValueError,KeyError,TypeError):
            warnings.append('source_conflict');rejected=True
    if tool=='search_code':
        for match in data.get('matches',[]):
            try:
                path=safe_relative(match['path'])
                if path not in sources:
                    warnings.append('unbound_source');continue
                lines=source_lines(sources[path]);number=match['line_number']
                if type(number) is not int or not 1<=number<=len(lines) or lines[number-1].strip()!=match['line']:
                    raise ValueError()
            except (ValueError,KeyError,TypeError):
                warnings.append('reference_invalid');rejected=True
    if tool=='query_graph':
        warnings.append('relation_unknown')  # Static resolution never proves runtime behavior.
        nodes={node.id:node for node in trusted.graph.nodes} if trusted.graph else {}
        for node in data.get('nodes',[]):
            original=nodes.get(node.get('id'))
            if original is None or any(node.get(k)!=getattr(original,k) for k in ('path','name','type')):
                warnings.append('reference_invalid');rejected=True
            elif any(k in node and node[k]!=getattr(original,k) for k in ('start_line','end_line','symbol_key')):
                warnings.append('reference_invalid');rejected=True
        for edge in data.get('edges',[]):
            if not any(all(edge.get(k)==getattr(original,k) for k in ('source','target','type'))
                       and (edge.get('site_id') is None or edge['site_id']==original.site_id)
                       for original in (trusted.graph.edges if trusted.graph else [])):
                warnings.append('reference_invalid');rejected=True
        if data.get('query_type')=='definitions' and not data.get('nodes'):warnings.append('definition_missing')
    return list(dict.fromkeys(warnings)),rejected


def verify_context_answer(answer, report, warnings=(), *, rejected=False, references=None):
    import re
    refs=references if references is not None else (report or {}).get('references',[])
    issues=list(warnings)
    addresses=re.findall(r'(?<![\w/])([\w./-]+\.py)(?::L?|#L)(\d+)(?:[-–]L?(\d+))?',answer or '')
    for path,start,end in addresses:
        start,end=int(start),int(end or start)
        if not any(r.get('path')==path and type(r.get('start_line')) is int
                   and r['start_line']<=start<=end<=r['end_line'] for r in refs):
            issues.append('citation_invalid')
    code=[r for r in refs if r['source'] in {'snapshot_source','static_graph','unbound_source'}]
    if answer and code and not addresses and not any(r.get('path') and r['path'] in answer for r in code):
        issues.append('citation_missing')
    if not code and any(r['source']=='memory_hint' for r in refs):issues.append('memory_hint_only')
    return dict(schema_version=1,status='rejected' if rejected else 'limited' if issues else 'checked' if code else 'unavailable',
                warnings=list(dict.fromkeys(issues)),natural_language_verified=False)


def collect_agent_evidence(result: AgentResult) -> list[str]:
    evidence: list[str] = []
    if result.memory is not None:
        for item in result.memory.relevant_memories:
            if item.path:
                evidence.append(f"{DEFAULT_MEMORY_DIR}/{item.path}")
        for item in result.memory.extracted_memories:
            if item.path:
                evidence.append(f"{DEFAULT_MEMORY_DIR}/{item.path}")
    for tool_result in result.tool_results:
        data = tool_result.data
        for item in data.get("evidence", []):
            evidence.append(str(item))
        for item in data.get("files", []):
            evidence.append(str(item))
        if data.get("path"):
            evidence.append(str(data["path"]))
        for item in data.get("matches", []):
            path = item.get("path")
            line_number = item.get("line_number")
            if path and line_number:
                evidence.append(f"{path}:{line_number}")
            elif path:
                evidence.append(str(path))
        for item in data.get("items", []):
            path = item.get("path")
            if path:
                evidence.append(str(path))
            evidence.extend(str(node_id) for node_id in item.get("node_ids", []))
            evidence.extend(str(edge) for edge in item.get("edges", []))
        for edge in data.get("edges", []):
            source = edge.get("source")
            edge_type = edge.get("type")
            target = edge.get("target")
            if source and edge_type and target:
                evidence.append(f"{source} --{edge_type}--> {target}")
        for node in data.get("nodes", []):
            node_id = node.get("id")
            if node_id:
                evidence.append(str(node_id))
    return dedupe_preserve_order(evidence)
