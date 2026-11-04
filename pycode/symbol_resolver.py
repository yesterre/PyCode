"""Project-root static import bindings, shared by Graph construction/validation.

Only recorded lexical and closed nominal receiver facts are consulted. Never
imports source or performs general type inference. Every lookup is bounded.
"""
from dataclasses import dataclass
from unicodedata import normalize

MAX_DEPTH = 16
MAX_CHECKS = 128
CAPABILITIES = ('import_bindings', 'cross_module_calls', 'symbol_references')
FLOW_CAPABILITIES = ('local_receiver_bindings',)


def module_name(path):
    if path == '__init__.py':
        return ''
    suffix = '/__init__.py' if path.endswith('/__init__.py') else '.py'
    return path[:-len(suffix)].replace('/', '.')


def dotted(expression):
    parts = [normalize('NFKC', p.strip()) for p in expression.split('.')]
    return parts if len(parts) <= 32 and all(p.isidentifier() for p in parts) else []


def binding_facts(facts, scope_ids):
    ids = {d['key']: d['id'] for d in facts['definitions']}
    return dict(path=facts['path'], dynamic=facts['dynamic'], blocked_names=facts['blocked_names'],
                scopes=[dict(id=scope_ids[s['key']], bindings={name: [
                    dict(kind=b['kind'], target=ids[b['definition']] if b['kind']=='definition' else
                         b['definition'] if b['kind']=='import' else None, conditional=b['conditional'])
                    for b in values] for name, values in sorted(s['bindings'].items())}) for s in facts['scopes']],
                definitions=[{k:d[k] for k in ('id','conditional','decorated','dynamic_class')}
                             for d in facts['definitions']])


def flow_facts(facts,scope_ids):
    def scoped(value):
        return {**{k:v for k,v in value.items() if k!='scope'},'scope_id':scope_ids[value['scope']]}
    return dict(path=facts['path'],assignments=[scoped(a) for a in facts['assignments']],
                mutations=[scoped(m) for m in facts['mutations']],escapes=[scoped(e) for e in facts['escapes']],
                methods=[dict(id=scope_ids[m['key']],**{k:v for k,v in m.items() if k!='key'}) for m in facts['methods']])


class Modules:
    def __init__(self, paths):
        self.paths = {}
        self.namespaces = set()
        for path in sorted(paths):
            components=path[:-3].split('/')
            if not all(p.isidentifier() and p==normalize('NFKC',p) for p in components):
                continue  # A dot in a filename is not a package directory.
            name = module_name(path)
            if not name:
                continue
            self.paths.setdefault(name, []).append(path)
            parts = name.split('.')
            self.namespaces.update('.'.join(parts[:i]) for i in range(1,len(parts)))

    def lookup(self, name):
        if not name or len(name.split('.')) > 32:
            return None, 'module_root_unknown'
        parts = name.split('.')
        for i in range(1,len(parts)+1):
            prefix = '.'.join(parts[:i]); paths = self.paths.get(prefix, [])
            if len(paths)>1:
                return None, 'ambiguous_module'
            if i<len(parts) and paths and not paths[0].endswith('/__init__.py'):
                return None, 'invalid_package_root'
        paths = self.paths.get(name, [])
        if paths:
            return paths[0], None
        return (None, None) if name in self.namespaces else (None, 'module_not_in_snapshot')

    def absolute(self, declaration):
        if not declaration['level']:
            return declaration['module'] or ''
        path=declaration['path']; name=module_name(path)
        if self.paths.get(name)!=[path]:
            return None
        package=name.split('.') if path.endswith('/__init__.py') else name.split('.')[:-1]
        if declaration['level']>len(package) or not package:
            return None
        base=package[:len(package)-declaration['level']+1]
        return '.'.join([*base,*((declaration['module'] or '').split('.') if declaration['module'] else [])])


@dataclass(frozen=True)
class Ref:
    kind: str
    target: str
    chain: tuple = ()
    flow: tuple = ()


class Uncertain(Exception):
    def __init__(self, basis, candidates=()):
        self.basis, self.candidates = basis, tuple(candidates)


class Lookup:
    def __init__(self):
        self.checks=0

    def tick(self, depth=0):
        self.checks+=1
        if depth>=MAX_DEPTH or self.checks>MAX_CHECKS:
            raise Uncertain('import_lookup_budget_exceeded')


class SymbolResolver:
    def __init__(self, nodes, scopes, declarations, facts, flows=None):
        self.nodes={n.id:n for n in nodes}
        self.scopes={s['id']:s for s in scopes}
        self.declarations={d['id']:d for d in declarations}
        self.files={f['path']:f for f in facts}
        self.bindings={s['id']:s['bindings'] for f in facts for s in f['scopes']}
        self.flags={d['id']:d for f in facts for d in f['definitions']}
        self.modules=Modules(self.files)
        self.has_flow=flows is not None
        self.assignments={};self.scope_assignments={};self.methods={};self.mutations={};self.escaped={}
        aliases={}
        def owner(scope_id,name):
            scope=self.scopes[scope_id]
            for _ in range(MAX_CHECKS):
                if self.bindings[scope['id']].get(name):return (scope['id'],name)
                scope=self.scopes.get(scope['parent_id'])
                while scope is not None and scope['kind']=='class':scope=self.scopes.get(scope['parent_id'])
                if scope is None:break
            return None
        for f in flows or ():
            self.methods.update({m['id']:m for m in f['methods']})
            for a in f['assignments']:
                self.assignments.setdefault((a['scope_id'],tuple(a['target'])),[]).append(a)
                self.scope_assignments.setdefault(a['scope_id'],[]).append(a)
                if len(a['target'])==1 and a['value']['kind']=='name':
                    source=owner(a['scope_id'],a['value']['parts'][0])
                    if source:aliases.setdefault((a['scope_id'],a['target'][0]),set()).add(source)
            for m in f['mutations']:
                self.mutations.setdefault(m['attribute'],[]).append(m)
            for e in f['escapes']:
                for name in e['names']:
                    bound=owner(e['scope_id'],name)
                    if bound:self.escaped.setdefault(bound[0],set()).add(bound[1])
        # A visible alias passed to unknown code invalidates its source too.
        # A visited worklist makes cyclic aliases linear, without following calls.
        pending=[(s,n) for s,names in self.escaped.items() for n in names];visited=set(pending)
        while pending:
            for source in aliases.get(pending.pop(),()):
                if source not in visited:
                    visited.add(source);pending.append(source)
                    self.escaped.setdefault(source[0],set()).add(source[1])

    def _namespace(self, scope, name):
        facts=self.files[scope['path']]
        if facts['dynamic'] or name in facts['blocked_names']:
            raise Uncertain('dynamic_or_nonlocal_namespace')
        if scope['kind']=='opaque':
            raise Uncertain('opaque_scope')

    def _lookup(self, scope_id, name, lookup, trail=(), site=None, export=False):
        origin=self.scopes[scope_id]; scope=origin
        while scope is not None:
            lookup.tick(len(trail));self._namespace(scope,name)
            values=self.bindings[scope['id']].get(name, [])
            if values:
                if len(values)>64:
                    raise Uncertain('import_lookup_budget_exceeded')
                targets=[v['target'] for v in values if v['kind']=='definition']
                if len(targets)>1:
                    raise Uncertain('multiple_lexical_definitions', sorted(targets))
                if self.has_flow and len(values)==1 and values[0]['kind']=='write':
                    return self._assignment_ref(scope,origin,name,lookup,trail,site)
                if self.has_flow and len(values)==1 and values[0]['kind']=='parameter':
                    return self._receiver(scope,origin,name,lookup)
                if len(values)!=1 or values[0]['kind'] not in {'definition','import'}:
                    raise Uncertain('binding_not_a_unique_definition',targets)
                value=values[0]
                if value['kind']=='definition':
                    target=self.nodes[value['target']]; flags=self.flags[target.id]
                    if self.has_flow and target.type=='class' and name in self.escaped.get(scope['id'],()):
                        raise Uncertain('class_escaped')
                    if flags['conditional'] or flags['decorated']:
                        raise Uncertain('conditional_or_decorated_definition',(target.id,))
                    if flags['dynamic_class']:
                        raise Uncertain('class_binding_deferred',(target.id,))
                    if site and target.start_line>=site['start_line'] and (scope is origin or scope['kind']!='module'):
                        raise Uncertain('definition_not_available_at_site',(target.id,))
                    return Ref('definition',target.id)
                declaration=self.declarations[value['target']]
                if value['conditional'] or declaration['conditional']:
                    raise Uncertain('conditional_import_binding')
                if site and (scope is origin or scope['kind']!='module') and (
                    declaration['end_line'],declaration['end_col_offset'])>(site['start_line'],site['col_offset']):
                    raise Uncertain('import_not_available_at_site')
                token=(declaration['id'],name)
                if token in trail:
                    raise Uncertain('cyclic_import_binding')
                ref=self._import(declaration,name,lookup,(*trail,token))
                if self.has_flow and name in self.escaped.get(scope['id'],()) and (
                        ref.kind=='module' or ref.kind=='definition' and self.nodes[ref.target].type=='class'):
                    raise Uncertain('class_or_module_escaped')
                return Ref(ref.kind,ref.target,(token,*ref.chain),ref.flow)
            if export:
                return None
            scope=self.scopes.get(scope['parent_id'])
            while scope is not None and scope['kind']=='class':
                lookup.tick(len(trail));scope=self.scopes.get(scope['parent_id'])
        raise Uncertain('name_not_bound_in_module')

    def _module(self, name):
        _,error=self.modules.lookup(name)
        if error:
            raise Uncertain(error)
        parts=name.split('.')
        for i in range(1,len(parts)):
            path,_=self.modules.lookup('.'.join(parts[:i]))
            if path and (self.files[path]['dynamic'] or '__path__' in self.bindings['file:'+path]):
                raise Uncertain('dynamic_package_path')
        return Ref('module',name)

    def _member(self, module, name, lookup, trail):
        lookup.tick(len(trail))
        self._module(module)
        path,error=self.modules.lookup(module)
        if error:
            raise Uncertain(error)
        child=module+'.'+name
        child_path,child_error=self.modules.lookup(child)
        if path:
            ref=self._lookup('file:'+path,name,lookup,trail,export=True)
            if ref is not None:
                # Package exports may intentionally shadow a submodule. Never
                # choose a different definition based on filesystem order.
                if child_error is None and (ref.kind!='module' or ref.target!=child):
                    raise Uncertain('ambiguous_package_attribute')
                return ref
            if '__getattr__' in self.bindings['file:'+path]:
                raise Uncertain('dynamic_module_export')
        if child_error is None and (path is None or path.endswith('/__init__.py')):
            return Ref('module',child)
        raise Uncertain(child_error if child_error=='ambiguous_module' else 'export_not_statically_bound')

    def _import(self, declaration, local_name, lookup, trail):
        lookup.tick(len(trail))
        aliases=[a for a in declaration['names'] if (a['asname'] or
                 (a['name'].split('.')[0] if declaration['module'] is None and not declaration['level'] else a['name']))==local_name]
        if len(aliases)!=1 or aliases[0]['name']=='*':
            raise Uncertain('import_binding_ambiguous')
        alias=aliases[0]
        if declaration['module'] is None and not declaration['level']:
            self._module(alias['name'])
            return self._module(alias['name'] if alias['asname'] else alias['name'].split('.')[0])
        module=self.modules.absolute(declaration)
        if module is None:
            raise Uncertain('relative_import_outside_package')
        return self._member(module,alias['name'],lookup,trail)

    def resolve(self, site):
        lookup=Lookup();parts=dotted(site['expression']);chain=();flow=()
        try:
            if not parts:
                raise Uncertain('receiver_or_dynamic_expression_deferred')
            scope=self.scopes[site['scope_id']]
            self._namespace(scope,parts[0])
            try:
                ref=self._lookup(scope['id'],parts[0],lookup,site=site)
            except Uncertain as exc:
                if len(parts)>1 and exc.basis in {'binding_not_a_unique_definition','name_not_bound_in_module'}:
                    raise Uncertain('receiver_or_dynamic_expression_deferred') from exc
                raise
            chain=ref.chain
            flow=ref.flow
            for part in parts[1:]:
                if ref.kind!='module' and not self.has_flow:
                    raise Uncertain('receiver_or_dynamic_expression_deferred')
                member=self._member(ref.target,part,lookup,()) if ref.kind=='module' else self._object_member(ref,part,lookup,())
                chain=(*chain,*member.chain);flow=(*flow,*member.flow);ref=member
            if ref.kind!='definition':
                raise Uncertain('module_is_not_a_callable_definition')
            return self._result('static','nominal_receiver_or_alias_binding' if flow else 'import_binding_chain' if chain else
                                'unique_same_module_lexical_definition',[ref.target],chain,flow)
        except Uncertain as exc:
            return self._result('ambiguous' if len(exc.candidates)>1 else 'unknown',
                                exc.basis,list(exc.candidates),())

    def _result(self,status,basis,candidates,chain,flow=()):
        result=dict(resolution=status,basis=basis,target_id=candidates[0] if status=='static' else None,
                    candidate_ids=candidates[:8],candidate_count=len(candidates),
                    binding_chain=[dict(declaration_id=d,local_name=n) for d,n in chain])
        if self.has_flow:
            result['flow_chain']=list(dict.fromkeys(flow))
        return result

    def _descriptor(self,method,lookup):
        metadata=self.methods.get(method.id)
        if metadata is None or metadata['descriptor']=='unknown' or self.flags[method.id]['conditional']:
            raise Uncertain('method_descriptor_unknown')
        role=metadata['descriptor']
        if role in {'classmethod','staticmethod'}:
            scope=self.scopes[method.parent_id]
            while scope is not None:
                lookup.tick();self._namespace(scope,role)
                if self.bindings[scope['id']].get(role):raise Uncertain('builtin_descriptor_shadowed')
                scope=self.scopes.get(scope['parent_id'])
        if role!='staticmethod' and metadata['receiver'] is None:
            raise Uncertain('method_receiver_missing')
        return metadata

    def _mutation_owner(self,mutation):
        method=self.methods.get(mutation['scope_id'])
        if method and method['descriptor']=='instance' and mutation['receiver']==[method['receiver']]:
            return self.nodes[method['id']].parent_id
        return None

    def _writes(self,class_id,name):
        return [m for m in self.mutations.get(name,()) if self._mutation_owner(m) in {None,class_id}]

    def _class_ok(self,class_id,lookup):
        lookup.tick();node=self.nodes[class_id];flags=self.flags[class_id]
        if node.name.rsplit('.',1)[-1] in self.escaped.get(node.parent_id,()):
            raise Uncertain('class_escaped')
        self._namespace(self.scopes[class_id],node.name.rsplit('.',1)[-1])
        if flags['dynamic_class'] or flags['decorated'] or flags['conditional']:
            raise Uncertain('class_binding_deferred')
        namespace=self.bindings[class_id]
        if any(name in namespace for name in ('__new__','__getattribute__','__getattr__','__setattr__','__delattr__')):
            raise Uncertain('dynamic_instance_hooks')
        if any(self._writes(class_id,name) for name in ('__class__','__bases__','__dict__','__mro__')):
            raise Uncertain('dynamic_class_mutation')

    def _initializer(self,class_id,lookup,trail=()):
        token=('initializer',class_id);lookup.tick(len(trail))
        if token in trail:raise Uncertain('cyclic_initializer')
        entries=self.bindings[class_id].get('__init__',[])
        if not entries:return None
        if len(entries)!=1 or entries[0]['kind']!='definition':raise Uncertain('initializer_not_simple')
        method=self.nodes[entries[0]['target']];meta=self._descriptor(method,lookup)
        if meta['descriptor']!='instance' or not meta['simple_init'] or meta['receiver'] in self.escaped.get(method.id,()):
            raise Uncertain('initializer_not_simple')
        for event in self.scope_assignments.get(method.id,()):
            lookup.tick(len(trail));target=event['target']
            if len(target)==2 and target[-1] in self.bindings[class_id]:raise Uncertain('dynamic_data_descriptor')
            if event['value']['kind']=='call':
                self._expression_ref(event['value'],method.id,event,lookup,(*trail,token))
        return meta

    def _receiver(self,scope,origin,name,lookup):
        meta=self.methods.get(scope['id'])
        if scope is not origin or not meta or meta['receiver']!=name or meta['descriptor']=='staticmethod':
            raise Uncertain('binding_not_a_unique_definition')
        method=self.nodes[meta['id']];self._descriptor(method,lookup)
        if name in self.escaped.get(scope['id'],()):raise Uncertain('receiver_escaped')
        class_id=method.parent_id;self._class_ok(class_id,lookup)
        return Ref('definition' if meta['descriptor']=='classmethod' else 'instance',class_id,flow=(method.id,))

    def _expression_ref(self,value,scope_id,site,lookup,trail):
        parts=value['parts']
        if not parts or value['kind']=='unknown':raise Uncertain('unsupported_flow_expression')
        ref=self._lookup(scope_id,parts[0],lookup,trail,site=site)
        chain=ref.chain;flow=ref.flow
        for part in parts[1:]:
            member=self._member(ref.target,part,lookup,trail) if ref.kind=='module' else self._object_member(ref,part,lookup,trail)
            chain=(*chain,*member.chain);flow=(*flow,*member.flow);ref=member
        if value['kind']=='call':
            if ref.kind!='definition' or self.nodes[ref.target].type!='class':raise Uncertain('factory_result_deferred')
            self._class_ok(ref.target,lookup);self._initializer(ref.target,lookup,trail)
            return Ref('instance',ref.target,chain,flow)
        return Ref(ref.kind,ref.target,chain,flow)

    def _assignment_ref(self,scope,origin,name,lookup,trail,site):
        events=self.assignments.get((scope['id'],(name,)),())
        if len(events)!=1:raise Uncertain('binding_not_a_unique_definition')
        event=events[0];token=('flow',event['id']);lookup.tick(len(trail))
        if token in trail:raise Uncertain('cyclic_flow_binding')
        if event['conditional'] or event['operation']!='assign':raise Uncertain('conditional_or_invalidated_flow')
        if scope is not origin and scope['kind']!='module':raise Uncertain('nonlocal_flow_deferred')
        if scope['kind']=='class':raise Uncertain('class_scope_flow_deferred')
        if site and (scope is origin or scope['kind']!='module') and (
                event['end_line'],event['end_col_offset'])>(site['start_line'],site['col_offset']):
            raise Uncertain('assignment_not_available_at_site')
        try:ref=self._expression_ref(event['value'],scope['id'],event,lookup,(*trail,token))
        except Uncertain as exc:
            if exc.basis=='name_not_bound_in_module':raise Uncertain('binding_not_a_unique_definition') from exc
            raise
        if name in self.escaped.get(scope['id'],()):raise Uncertain('receiver_escaped')
        return Ref(ref.kind,ref.target,ref.chain,(*ref.flow,event['id']))

    def _object_member(self,ref,name,lookup,trail):
        lookup.tick(len(trail))
        if ref.kind=='instance':class_id=ref.target
        elif ref.kind=='definition' and self.nodes[ref.target].type=='class':class_id=ref.target
        else:raise Uncertain('receiver_or_dynamic_expression_deferred')
        self._class_ok(class_id,lookup)
        entries=self.bindings[class_id].get(name,())
        writes=self._writes(class_id,name)
        if entries:
            if len(entries)!=1 or entries[0]['kind']!='definition' or writes:raise Uncertain('attribute_write_conflict')
            method=self.nodes[entries[0]['target']]
            if method.type!='method':raise Uncertain('method_descriptor_unknown')
            self._descriptor(method,lookup)
            return Ref('definition',method.id,flow=(method.id,))
        if ref.kind!='instance':raise Uncertain('class_attribute_not_bound')
        init=self._initializer(class_id,lookup,trail)
        if not init:raise Uncertain('instance_attribute_not_bound')
        events=self.assignments.get((init['id'],(init['receiver'],name)),())
        if len(events)!=1 or len(writes)!=1 or writes[0]['id']!=events[0]['mutation_id']:
            raise Uncertain('attribute_write_conflict')
        event=events[0];token=('field',event['id'])
        if event['conditional'] or event['operation']!='assign' or event['value']['kind']!='call':
            raise Uncertain('initialization_not_a_constructor')
        if token in trail:raise Uncertain('cyclic_flow_binding')
        value=self._expression_ref(event['value'],init['id'],event,lookup,(*trail,token))
        return Ref(value.kind,value.target,value.chain,(*value.flow,init['id'],event['id']))

    def declaration_target(self, declaration, alias):
        """File dependency only, deliberately separate from symbol binding."""
        module=self.modules.absolute(declaration)
        if module is None:
            return None
        names=[alias['name']] if declaration['module'] is None and not declaration['level'] else [module+'.'+alias['name'],module]
        for name in names:
            path,error=self.modules.lookup(name)
            if path and error is None:
                return path
        return None


def validate_binding_facts(graph,nodes,scopes,declarations):
    """Validate the persisted namespaces before resolving any untrusted record.

    Schema3 calls are recomputed from this same resolver, not accepted merely
    because their target happens to be a valid node in another file.
    """
    from collections import Counter
    def require(value):
        if not value:
            raise ValueError('Invalid Graph binding facts')
    require(isinstance(graph.binding_facts,list))
    file_paths={f['path'] for f in graph.files};seen=set();scope_ids=set();definitions=set()
    actual_imports=Counter();blocked_by_path={}
    expected_definitions={path:Counter() for path in file_paths}
    flags_by_id={}
    for node in nodes.values():
        if node.type in {'function','class','method'}:
            expected_definitions[node.path][node.id]=1
    for facts in graph.binding_facts:
        require(isinstance(facts,dict) and set(facts)=={'path','dynamic','blocked_names','scopes','definitions'})
        path=facts['path'];require(path in file_paths and path not in seen);seen.add(path)
        require(type(facts['dynamic']) is bool and isinstance(facts['blocked_names'],list))
        require(all(isinstance(n,str) and n.isidentifier() for n in facts['blocked_names']))
        blocked=set();counts=Counter()
        require(isinstance(facts['definitions'],list) and isinstance(facts['scopes'],list))
        for d in facts['definitions']:
            require(isinstance(d,dict) and set(d)=={'id','conditional','decorated','dynamic_class'})
            node=nodes.get(d['id']);require(node is not None and node.path==path and node.type in {'function','class','method'})
            require(d['id'] not in definitions and all(type(d[k]) is bool for k in ('conditional','decorated','dynamic_class')))
            definitions.add(d['id'])
            flags_by_id[d['id']]=d
        for scope in facts['scopes']:
            require(isinstance(scope,dict) and set(scope)=={'id','bindings'} and isinstance(scope['bindings'],dict))
            sid=scope['id'];require(sid in scopes and scopes[sid]['path']==path and sid not in scope_ids);scope_ids.add(sid)
            for name,values in scope['bindings'].items():
                require(isinstance(name,str) and (name=='*' or name.isidentifier()) and isinstance(values,list) and bool(values))
                for b in values:
                    require(isinstance(b,dict) and set(b)=={'kind','target','conditional'} and type(b['conditional']) is bool)
                    kind=b['kind'];require(kind in {'definition','import','parameter','write','global_or_nonlocal','type_parameter'})
                    if kind=='definition':
                        node=nodes.get(b['target'])
                        require(node is not None and node.parent_id==sid and node.name.rsplit('.',1)[-1]==name)
                        counts[node.id]+=1
                    elif kind=='import':
                        d=declarations.get(b['target']);require(d is not None and d['scope_id']==sid and b['conditional']==d['conditional'])
                        actual_imports[sid,name,d['id']]+=1
                    else:
                        require(b['target'] is None)
                        if kind in {'global_or_nonlocal','type_parameter'}:
                            blocked.add(name)
        require(blocked==set(facts['blocked_names']) and len(blocked)==len(facts['blocked_names']))
        require(counts==expected_definitions[path])
        blocked_by_path[path]=facts['dynamic']
    require(seen==file_paths and scope_ids==set(scopes))
    require(definitions=={n.id for n in nodes.values() if n.type in {'function','class','method'}})
    expected=Counter()
    for d in declarations.values():
        for alias in d['names']:
            name=alias['asname'] or (alias['name'].split('.')[0] if d['module'] is None and not d['level'] else alias['name'])
            expected[d['scope_id'],name,d['id']]+=1
            if name=='*':require(blocked_by_path[d['path']])
    require(actual_imports==expected)
    for f in graph.binding_facts:
        for s in f['scopes']:
            for values in s['bindings'].values():
                for b in values:
                    if b['kind']=='definition':
                        require(b['conditional']==flags_by_id[b['target']]['conditional'])
    from pycode.structure import CAPABILITIES as BASE
    require(set(graph.capabilities)==set(BASE+CAPABILITIES+(FLOW_CAPABILITIES if graph.schema_version==4 else ())))
    if graph.schema_version==4:
        validate_flow_facts(graph,nodes,scopes)
    return SymbolResolver(graph.nodes,graph.scopes,graph.import_declarations,graph.binding_facts,
                          graph.flow_facts if graph.schema_version==4 else None)


def validate_flow_facts(graph,nodes,scopes):
    """Closed, source-located flow IR. Unsupported writes never become types."""
    from collections import Counter
    from pycode.structure import identity
    def require(value):
        if not value:raise ValueError('Invalid Graph flow facts')
    files={f['path']:f for f in graph.files};sites={c['id']:c for c in graph.call_sites}
    bindings={s['id']:s['bindings'] for f in graph.binding_facts for s in f['scopes']}
    flags={d['id']:d for f in graph.binding_facts for d in f['definitions']}
    require(isinstance(graph.flow_facts,list))
    seen=set();methods=set();ids=set();write_counts=Counter()
    def path(value,empty=False):
        return isinstance(value,list) and (empty or bool(value)) and len(value)<=32 and all(isinstance(p,str) and p.isidentifier() for p in value)
    def location(item,file):
        sid=item['scope_id'];require(sid in scopes and scopes[sid]['path']==file)
        require(all(type(item[k]) is int for k in ('start_line','end_line','col_offset','end_col_offset')))
        require(scopes[sid]['start_line']<=item['start_line']<=item['end_line']<=scopes[sid]['end_line'])
        require(1<=item['start_line'] and item['end_line']<=files[file]['line_count'] and item['col_offset']>=0 and item['end_col_offset']>=0)
        require(item['start_line']<item['end_line'] or item['col_offset']<item['end_col_offset'])
    for facts in graph.flow_facts:
        require(isinstance(facts,dict) and set(facts)=={'path','methods','assignments','mutations','escapes'})
        file=facts['path'];require(file in files and file not in seen);seen.add(file)
        require(all(isinstance(facts[k],list) for k in ('methods','assignments','mutations','escapes')))
        for m in facts['methods']:
            require(isinstance(m,dict) and set(m)=={'id','receiver','descriptor','simple_init'})
            node=nodes.get(m['id']);require(node is not None and node.type=='method' and node.path==file and node.id not in methods)
            methods.add(node.id)
            require(type(m['simple_init']) is bool and m['descriptor'] in {'instance','staticmethod','classmethod','unknown'})
            require(m['receiver'] is None or isinstance(m['receiver'],str) and m['receiver'].isidentifier() and
                    any(v['kind']=='parameter' for v in bindings[node.id].get(m['receiver'],())))
            require(flags[node.id]['decorated']==(m['descriptor']!='instance'))
        mutations={}
        for m in facts['mutations']:
            require(isinstance(m,dict) and set(m)=={'id','scope_id','attribute','receiver','start_line','end_line','col_offset','end_col_offset'})
            location(m,file);require(isinstance(m['attribute'],str) and m['attribute'].isidentifier() and path(m['receiver'],True))
            require(m['id']==identity('mutation',file,scopes[m['scope_id']]['key'],m['start_line'],m['col_offset'],m['end_line'],m['end_col_offset'],m['attribute']) and m['id'] not in ids)
            ids.add(m['id']);mutations[m['id']]=m
        for a in facts['assignments']:
            require(isinstance(a,dict) and set(a)=={'id','scope_id','start_line','end_line','col_offset','end_col_offset','target','value','conditional','operation','statement','mutation_id'})
            location(a,file);require(path(a['target']) and len(a['target'])<=2 and type(a['conditional']) is bool)
            require(a['operation'] in {'assign','invalidate'} and a['statement'] in {'Assign','AnnAssign','AugAssign','Delete'})
            require((a['operation']=='assign')==(a['statement'] in {'Assign','AnnAssign'}))
            require(a['id']==identity('flow',file,scopes[a['scope_id']]['key'],a['statement'],a['start_line'],a['col_offset'],a['target']) and a['id'] not in ids)
            ids.add(a['id']);v=a['value']
            require(isinstance(v,dict) and set(v)=={'kind','parts'} and v['kind'] in {'name','call','unknown'} and path(v['parts'],v['kind']=='unknown'))
            require(v['kind']!='unknown' or not v['parts'])
            if len(a['target'])==1:
                require(a['mutation_id'] is None)
                write_counts[a['scope_id'],a['target'][0],a['conditional']]+=1
            else:
                m=mutations.get(a['mutation_id'])
                require(m is not None and m['scope_id']==a['scope_id'] and m['receiver']==a['target'][:-1] and m['attribute']==a['target'][-1])
                require(a['start_line']<=m['start_line']<=m['end_line']<=a['end_line'])
        for e in facts['escapes']:
            require(isinstance(e,dict) and set(e)=={'call_id','scope_id','names'} and e['call_id'] in sites)
            require(sites[e['call_id']]['scope_id']==e['scope_id'] and scopes[e['scope_id']]['path']==file)
            require(isinstance(e['names'],list) and bool(e['names']) and all(isinstance(n,str) and n.isidentifier() for n in e['names']) and len(set(e['names']))==len(e['names']))
    require(seen==set(files) and methods=={n.id for n in nodes.values() if n.type=='method'})
    available=Counter((sid,name,b['conditional']) for sid,values in bindings.items() for name,items in values.items() for b in items if b['kind']=='write')
    require(not (write_counts-available))
