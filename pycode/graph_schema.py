"""Versioned Graph codec/invariants and bounded metadata projections.

Snapshot Manifest versions are independent. V1 is read/written in its exact
legacy shape; v2 is explicit and carries static facts, not runtime guarantees.
"""
from dataclasses import asdict, fields
import re

from pycode.models import CodeGraph, GraphNode, GraphEdge
from pycode.utils import normalize_path


def _require(condition, message='Invalid Graph structure'):
    if not condition:
        raise ValueError(message)


def _path(path):
    return (isinstance(path,str) and bool(path) and path==normalize_path(path)
            and not path.startswith('/') and ':' not in path and '\x00' not in path
            and all(p not in {'','.','..'} for p in path.split('/')))


def _integer(value, minimum=0):
    return type(value) is int and value>=minimum


def _text(value):
    return isinstance(value,str) and bool(value)


def _range(value, files, *, empty_module=False):
    _require(value['path'] in files)
    start,end = value['start_line'],value['end_line']
    _require(_integer(start,1) and _integer(end) and end<=files[value['path']]['line_count'])
    _require(start<=end or (empty_module and start==1 and end==0))


def validate_graph(graph, binding=None, chunks=None):
    _require(type(graph.schema_version) is int and graph.schema_version in {1,2,3,4}, 'Unsupported Graph schema version')
    if graph.schema_version==1:
        return  # Legacy facts retain their original limitations, never invented metadata.
    _require(_text(graph.project_path) and _text(graph.producer))
    _require(isinstance(graph.capabilities,(tuple,list)) and len(set(graph.capabilities))==len(graph.capabilities))
    _require(all(isinstance(c,str) and re.fullmatch('[a-z_]+',c) for c in graph.capabilities))
    if graph.binding is not None:
        from pycode.chunks import SnapshotBinding
        _require(isinstance(graph.binding,dict) and set(graph.binding)=={'project_id','snapshot_id','commit_sha'})
        SnapshotBinding(**graph.binding)
    if binding is not None:
        _require(graph.binding==asdict(binding), 'Graph Snapshot binding mismatch')
    files = {}
    source_lines = {}
    for f in graph.files:
        _require(isinstance(f,dict) and set(f)=={'path','source_sha256','line_count'})
        _require(_path(f['path']) and f['path'] not in files and _integer(f['line_count']))
        _require(isinstance(f['source_sha256'],str) and re.fullmatch('[0-9a-f]{64}',f['source_sha256']) is not None)
        files[f['path']]=f
    if chunks is not None:
        _require({f.path:(f.content_hash,f.line_count) for f in chunks.files}==
                 {p:(f['source_sha256'],f['line_count']) for p,f in files.items()}, 'Graph/Chunk source mismatch')
        ranges={(c.path,c.span.symbol_key,c.span.symbol_start_line,c.span.symbol_end_line) for c in chunks.chunks}
        _require(all((n.path,n.symbol_key,n.start_line,n.end_line) in ranges for n in graph.nodes
                     if n.type in {'function','class','method'}), 'Graph/Chunk symbol mismatch')
        parts={path:[] for path in files}
        for chunk in sorted(chunks.chunks,key=lambda c:(c.path,c.span.start_line)):
            parts[chunk.path].append(chunk.content)
        source_lines={p:''.join(values).encode('utf-8').split(b'\n') for p,values in parts.items()}
    nodes = {}
    for n in graph.nodes:
        _require(_text(n.id) and n.id not in nodes and _text(n.name))
        _require(n.type in {'file','class','function','method','external'})
        _require(n.path is None if n.type=='external' else n.path in files)
        if n.type=='file':
            _require(n.id=='file:'+n.path and n.symbol_key=='<module>' and n.parent_id is None)
        elif n.type!='external':
            _require(_text(n.symbol_key) and _text(n.parent_id))
            _range(asdict(n),files)
        nodes[n.id]=n
    _require({n.path for n in graph.nodes if n.type=='file'}==set(files))
    scopes = {}
    scope_fields={'id','path','key','parent_id','kind','name','start_line','end_line'}
    for s in graph.scopes:
        _require(isinstance(s,dict) and set(s)==scope_fields and _text(s['id']) and s['id'] not in scopes)
        _require(_text(s['key']) and _text(s['name']) and s['kind'] in {'module','class','function','method','opaque'})
        _range(s,files,empty_module=s['kind']=='module')
        if s['kind']=='module':
            _require(s['id']=='file:'+s['path'] and s['parent_id'] is None and s['key']=='<module>')
        elif s['kind']!='opaque':
            n=nodes.get(s['id'])
            _require(n is not None and (n.path,n.type,n.symbol_key,n.parent_id,n.start_line,n.end_line)==
                     (s['path'],s['kind'],s['key'],s['parent_id'],s['start_line'],s['end_line']))
        scopes[s['id']]=s
    _require({s['path'] for s in scopes.values() if s['kind']=='module'}==set(files))
    _require({s['id'] for s in scopes.values() if s['kind']!='opaque'}==
             {n.id for n in graph.nodes if n.type!='external'})
    _require(all(s['id'] not in nodes for s in scopes.values() if s['kind']=='opaque'))
    for s in scopes.values():
        seen=set(); current=s
        while current['parent_id'] is not None:
            _require(current['id'] not in seen and current['parent_id'] in scopes)
            seen.add(current['id']);parent=scopes[current['parent_id']]
            _require(parent['path']==s['path'] and parent['start_line']<=current['start_line'] and parent['end_line']>=current['end_line'])
            current=parent
    sites = {}
    call_fields={'id','path','scope_id','caller_id','start_line','end_line','col_offset','end_col_offset',
                 'expression','resolution','basis','target_id','candidate_ids','candidate_count'}
    if graph.schema_version in {3,4}:
        call_fields.add('binding_chain')
    if graph.schema_version==4:
        call_fields.add('flow_chain')
    for c in graph.call_sites:
        _require(isinstance(c,dict) and set(c)==call_fields and _text(c['id']) and c['id'] not in sites)
        _range(c,files)
        _require(c['scope_id'] in scopes and scopes[c['scope_id']]['path']==c['path'])
        _require(c['caller_id'] in nodes and nodes[c['caller_id']].path==c['path'])
        scope=scopes[c['scope_id']]
        _require(scope['start_line']<=c['start_line']<=c['end_line']<=scope['end_line'])
        while scope['kind']=='opaque':
            scope=scopes[scope['parent_id']]
        _require(scope['id']==c['caller_id'])
        _require(_integer(c['col_offset']) and _integer(c['end_col_offset']) and _text(c['expression']))
        _require(c['start_line']<c['end_line'] or c['col_offset']<c['end_col_offset'])
        if source_lines:
            lines=source_lines[c['path']]
            first,last=c['start_line']-1,c['end_line']-1
            _require(c['col_offset']<=len(lines[first]) and c['end_col_offset']<=len(lines[last]), 'Call position exceeds source')
            segment=(lines[first][c['col_offset']:c['end_col_offset']] if first==last else
                     b'\n'.join([lines[first][c['col_offset']:],*lines[first+1:last],lines[last][:c['end_col_offset']]]))
            _require(c['expression'].encode('utf-8') in segment, 'Call expression differs from source')
        from pycode.structure import identity
        _require(c['id']==identity('call',c['path'],scopes[c['scope_id']]['key'],c['start_line'],
                                    c['col_offset'],c['end_line'],c['end_col_offset']))
        _require(c['resolution'] in {'static','unknown','ambiguous'} and isinstance(c['basis'],str)
                 and re.fullmatch('[a-z_]+',c['basis']) is not None)
        candidates=c['candidate_ids']
        _require(isinstance(candidates,list) and len(candidates)<=8 and len(set(candidates))==len(candidates)
                 and _integer(c['candidate_count'],len(candidates)))
        _require(all(t in nodes and (graph.schema_version in {3,4} or nodes[t].path==c['path']) and nodes[t].type in {'function','class','method'} for t in candidates))
        if c['resolution']=='static':
            _require(c['candidate_count']==1 and candidates==[c['target_id']])
            if graph.schema_version==2:
                from unicodedata import normalize
                name=normalize('NFKC',c['expression'])
                _require(name.isidentifier() and nodes[c['target_id']].name.rsplit('.',1)[-1]==name)
                lexical=set();current=scopes[c['scope_id']]
                while current is not None:
                    lexical.add(current['id']);current=scopes.get(current['parent_id'])
                    while current is not None and current['kind']=='class':
                        current=scopes.get(current['parent_id'])
                _require(nodes[c['target_id']].parent_id in lexical)
        else:
            _require(c['target_id'] is None)
            if c['resolution']=='ambiguous':
                _require(c['candidate_count']>=2)
        sites[c['id']]=c
    declarations = {}
    import_fields={'id','path','scope_id','module','level','names','start_line','end_line','col_offset','end_col_offset','conditional'}
    for d in graph.import_declarations:
        _require(isinstance(d,dict) and set(d)==import_fields and _text(d['id']) and d['id'] not in declarations)
        _range(d,files)
        _require(d['scope_id'] in scopes and scopes[d['scope_id']]['path']==d['path'])
        _require(d['module'] is None or isinstance(d['module'],str))
        _require(_integer(d['level']) and type(d['conditional']) is bool and _integer(d['col_offset']) and _integer(d['end_col_offset']))
        _require(d['id']==identity('import',d['path'],scopes[d['scope_id']]['key'],d['start_line'],d['col_offset']))
        _require(isinstance(d['names'],list) and bool(d['names']))
        _require(all(isinstance(a,dict) and set(a)=={'name','asname'} and _text(a['name']) and
                     (a['asname'] is None or _text(a['asname'])) for a in d['names']))
        declarations[d['id']]=d
    if graph.schema_version in {3,4}:
        from pycode.symbol_resolver import validate_binding_facts
        resolver=validate_binding_facts(graph,nodes,scopes,declarations)
        flow_tokens=({a['id'] for f in graph.flow_facts for a in f['assignments']} |
                     {m['id'] for f in graph.flow_facts for m in f['methods']}) if graph.schema_version==4 else set()
        for c in sites.values():
            chain=c['binding_chain']
            _require(isinstance(chain,list) and len(chain)<=128 and all(isinstance(s,dict) and
                     set(s)=={'declaration_id','local_name'} and s['declaration_id'] in declarations and
                     _text(s['local_name']) for s in chain))
            if graph.schema_version==4:
                _require(isinstance(c['flow_chain'],list) and len(c['flow_chain'])<=128 and all(isinstance(i,str) and i in flow_tokens for i in c['flow_chain']))
            expected=resolver.resolve(c)
            _require(all(c[k]==v for k,v in expected.items()),'Call binding proof mismatch')
    keys=set(); linked=set(); containment=set(); references=set(); imports=set()
    for e in graph.edges:
        _require(e.source in nodes and e.target in nodes)
        key=(e.source,e.target,e.type,e.site_id)
        _require(key not in keys);keys.add(key)
        if e.type=='contains':
            _require(nodes[e.target].parent_id==e.source and e.resolution=='static' and e.basis=='lexical_containment')
            containment.add(e.target)
        elif e.type=='imports':
            d=declarations.get(e.site_id)
            _require(d is not None and e.source=='file:'+d['path'] and e.resolution=='declaration')
            _require((e.path,e.start_line,e.end_line)==(d['path'],d['start_line'],d['end_line']))
            _require(nodes[e.target].type in {'file','external'})
            imports.add((e.source,e.target,e.site_id))
        elif e.type=='references' and graph.schema_version in {3,4}:
            c=sites.get(e.site_id)
            _require(c is not None and c['resolution']=='static' and bool(c['binding_chain'] or c.get('flow_chain')))
            _require((e.source,e.target,e.path,e.start_line,e.end_line,e.resolution,e.basis)==
                     (c['caller_id'],c['target_id'],c['path'],c['start_line'],c['end_line'],c['resolution'],c['basis']))
            references.add(c['id'])
        elif e.type in {'calls','unresolved_call'}:
            c=sites.get(e.site_id)
            _require(c is not None and e.site_id not in linked);linked.add(e.site_id)
            _require((e.source,e.path,e.start_line,e.end_line,e.resolution,e.basis)==
                     (c['caller_id'],c['path'],c['start_line'],c['end_line'],c['resolution'],c['basis']))
            if e.type=='calls':
                _require(c['resolution']=='static' and e.target==c['target_id'])
            else:
                _require(c['resolution']!='static' and e.target=='unresolved:'+c['id'] and nodes[e.target].type=='external')
        else:
            raise ValueError('Unsupported Graph relation')
    _require(linked==set(sites))
    _require(containment=={n.id for n in graph.nodes if n.type in {'function','class','method'}})
    if graph.schema_version in {3,4}:
        _require(references=={c['id'] for c in sites.values() if c['resolution']=='static' and (c['binding_chain'] or c.get('flow_chain'))})
        expected=set()
        for d in declarations.values():
            module='.'*d['level']+(d['module'] or '')
            for alias in d['names']:
                name=module+('' if module.endswith('.') else '.')+alias['name'] if module else alias['name']
                path=resolver.declaration_target(d,alias)
                expected.add(('file:'+d['path'],'file:'+path if path else 'external:'+name,d['id']))
        _require(imports==expected,'Import declaration targets differ')


def node_data(node, *, details=False):
    value={k:getattr(node,k) for k in ('id','type','name','path')}
    if details:
        value.update({k:getattr(node,k) for k in ('symbol_key','parent_id','start_line','end_line')})
    return value


def edge_data(edge, *, details=False):
    value={k:getattr(edge,k) for k in ('source','target','type')}
    if details:
        value.update({k:getattr(edge,k) for k in ('site_id','path','start_line','end_line','resolution','basis')})
    return value


def graph_info(graph):
    return dict(graph_schema_version=graph.schema_version,
                graph_capabilities=list(graph.capabilities) if graph.schema_version in {2,3,4} else ['legacy_structure'],
                graph_warnings=[] if graph.schema_version in {2,3,4} else ['legacy_graph_unverified'])


def public_sites(graph, caller_ids, limit=20):
    values=[s for s in graph.call_sites if s['caller_id'] in caller_ids]
    result=[]
    for s in values[:limit]:
        item={k:s[k] for k in ('id','path','caller_id','start_line','end_line','resolution','basis','target_id','candidate_count')}
        if graph.schema_version in {3,4}:
            item.update(binding_declarations=[step['declaration_id'] for step in s['binding_chain'][:8]],
                        binding_chain_omitted=max(0,len(s['binding_chain'])-8))
        if graph.schema_version==4:
            item.update(flow_evidence=s['flow_chain'][:8],flow_evidence_omitted=max(0,len(s['flow_chain'])-8))
        result.append(item)
    return result,max(0,len(values)-limit)


def graph_to_dict(graph):
    validate_graph(graph)
    if graph.schema_version==1:
        return dict(project_path=graph.project_path,nodes=[node_data(n) for n in graph.nodes],
                    edges=[edge_data(e) for e in graph.edges])
    data=asdict(graph)
    data['capabilities']=list(graph.capabilities)
    if graph.schema_version==2:
        data.pop('binding_facts')
    if graph.schema_version<4:
        data.pop('flow_facts')
    return data


def graph_from_dict(data):
    try:
        return _graph_from_dict(data)
    except (KeyError,TypeError,AttributeError,OverflowError) as exc:
        raise ValueError('Invalid Graph structure') from exc


def _graph_from_dict(data):
    _require(isinstance(data,dict))
    version=data.get('schema_version',1)
    _require(type(version) is int and version in {1,2,3,4}, 'Unsupported Graph schema version')
    if version==1:
        return CodeGraph(data['project_path'],[GraphNode(**{k:n[k] for k in ('id','type','name')},path=n.get('path')) for n in data.get('nodes',[])],
                         [GraphEdge(**{k:e[k] for k in ('source','target','type')}) for e in data.get('edges',[])])
    expected={f.name for f in fields(CodeGraph)}-({'binding_facts'} if version==2 else set())-({'flow_facts'} if version<4 else set())
    _require(set(data)==expected)
    _require(isinstance(data['capabilities'],list))
    graph=CodeGraph(**{**data,'nodes':[GraphNode(**n) for n in data['nodes']],
                      'edges':[GraphEdge(**e) for e in data['edges']], 'capabilities':tuple(data['capabilities'])})
    validate_graph(graph)
    return graph
