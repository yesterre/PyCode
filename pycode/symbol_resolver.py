"""Project-root static import bindings, shared by Graph construction/validation.

Only recorded lexical facts are consulted. Never imports source or infers a
receiver type. Every lookup has independent deterministic depth/work bounds.
"""
from dataclasses import dataclass
from unicodedata import normalize

MAX_DEPTH = 16
MAX_CHECKS = 128
CAPABILITIES = ('import_bindings', 'cross_module_calls', 'symbol_references')


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
    def __init__(self, nodes, scopes, declarations, facts):
        self.nodes={n.id:n for n in nodes}
        self.scopes={s['id']:s for s in scopes}
        self.declarations={d['id']:d for d in declarations}
        self.files={f['path']:f for f in facts}
        self.bindings={s['id']:s['bindings'] for f in facts for s in f['scopes']}
        self.flags={d['id']:d for f in facts for d in f['definitions']}
        self.modules=Modules(self.files)

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
                if len(values)!=1 or values[0]['kind'] not in {'definition','import'}:
                    raise Uncertain('binding_not_a_unique_definition',targets)
                value=values[0]
                if value['kind']=='definition':
                    target=self.nodes[value['target']]; flags=self.flags[target.id]
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
                return Ref(ref.kind,ref.target,(token,*ref.chain))
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
        lookup=Lookup();parts=dotted(site['expression']);chain=()
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
            for part in parts[1:]:
                if ref.kind!='module':
                    raise Uncertain('receiver_or_dynamic_expression_deferred')
                member=self._member(ref.target,part,lookup,())
                chain=(*chain,*member.chain);ref=member
            if ref.kind!='definition':
                raise Uncertain('module_is_not_a_callable_definition')
            return self._result('static','import_binding_chain' if chain else
                                'unique_same_module_lexical_definition',[ref.target],chain)
        except Uncertain as exc:
            return self._result('ambiguous' if len(exc.candidates)>1 else 'unknown',
                                exc.basis,list(exc.candidates),())

    @staticmethod
    def _result(status,basis,candidates,chain):
        return dict(resolution=status,basis=basis,target_id=candidates[0] if status=='static' else None,
                    candidate_ids=candidates[:8],candidate_count=len(candidates),
                    binding_chain=[dict(declaration_id=d,local_name=n) for d,n in chain])

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
    require(set(graph.capabilities)==set(BASE+CAPABILITIES))
    return SymbolResolver(graph.nodes,graph.scopes,graph.import_declarations,graph.binding_facts)
