"""Compact lexical facts from an ALREADY parsed AST; never executes source.

Bindings deliberately over-approximate writes. The resolver consumes these facts;
no AST/source object is retained in them and no repository code is executed.
"""
import ast
from collections import Counter, defaultdict
import hashlib
import json

from pycode.utils import normalize_path


CAPABILITIES = ('lexical_scopes', 'call_sites', 'import_declarations', 'same_module_direct_calls')


def identity(kind, *values):
    data = json.dumps(values, ensure_ascii=False, separators=(',', ':'))
    return kind + ':' + hashlib.sha256(data.encode('utf-8')).hexdigest()


def collect_structure(tree, source, path):
    collector = _Collector(source, normalize_path(path))
    collector.visit(tree)
    return collector.finish()


class _Collector(ast.NodeVisitor):
    def __init__(self, source, path):
        self.source, self.path = source, path
        self.scopes, self.definitions, self.calls, self.imports = [], [], [], []
        self.assignments, self.mutations, self.methods, self.escapes = [], [], [], []
        self.by_key = {}
        self.counts = defaultdict(Counter)
        self.current = self.scope('<module>', None, 'module', '<module>', 1,
                                  len(source.split('\n')) - int(source.endswith('\n')) if source else 0)
        self.conditional = 0

    def scope(self, key, parent, kind, name, start, end):
        value = dict(key=key, parent=parent, kind=kind, name=name, start_line=start,
                     end_line=end, bindings={}, dynamic=False)
        self.scopes.append(value)
        self.by_key[key] = value
        return value

    def bind(self, name, kind, definition=None):
        self.current['bindings'].setdefault(name, []).append(
            dict(kind=kind, definition=definition, conditional=bool(self.conditional)))

    def definition(self, node, kind):
        parent = self.current
        self.counts[parent['key']][node.name] += 1
        occurrence = self.counts[parent['key']][node.name]
        key = f"{parent['key']}/{node.name}[{occurrence}]"
        name = node.name if parent['kind'] == 'module' else parent['name'] + '.' + node.name
        start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
        fact = dict(key=key, parent=parent['key'], name=name, short_name=node.name,
                    kind=kind, start_line=start, end_line=node.end_lineno,
                    conditional=bool(self.conditional), decorated=bool(node.decorator_list),
                    dynamic_class=isinstance(node,ast.ClassDef) and bool(node.bases or node.keywords))
        self.definitions.append(fact)
        self.bind(node.name, 'definition', key)
        # Defaults, annotations, bases and decorators execute in the enclosing
        # scope. They are not calls from the new function/method body.
        for decorator in node.decorator_list:
            self.visit(decorator)
        for parameter in getattr(node,'type_params',[]):
            self.opaque(parameter)
        if isinstance(node, ast.ClassDef):
            for value in [*node.bases, *node.keywords]:
                self.visit(value)
        else:
            self.visit(node.args)
            if node.returns:
                self.visit(node.returns)
        self.current = self.scope(key, parent['key'], kind, name, start, node.end_lineno)
        conditional = self.conditional
        self.conditional = 0
        for parameter in getattr(node,'type_params',[]):
            self.bind(parameter.name,'type_parameter')
        if not isinstance(node, ast.ClassDef):
            args = node.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs,
                        *([args.vararg] if args.vararg else []), *([args.kwarg] if args.kwarg else [])]:
                self.bind(arg.arg, 'parameter')
            if kind=='method':
                positional=[*args.posonlyargs,*args.args]
                receiver=positional[0].arg if positional else None
                descriptor='instance' if not node.decorator_list else (
                    node.decorator_list[0].id if len(node.decorator_list)==1 and
                    isinstance(node.decorator_list[0],ast.Name) and
                    node.decorator_list[0].id in {'staticmethod','classmethod'} else 'unknown')
                parameters={a.arg for a in [*args.posonlyargs,*args.args,*args.kwonlyargs]}
                self.methods.append(dict(key=key,receiver=receiver,descriptor=descriptor,
                                         simple_init=_simple_init(node.body,receiver,parameters)))
        for statement in node.body:
            self.visit(statement)
        self.current, self.conditional = parent, conditional

    def visit_FunctionDef(self, node):
        self.definition(node, 'method' if self.current['kind'] == 'class' else 'function')

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        self.definition(node, 'class')

    def visit_Call(self, node):
        expression = ast.get_source_segment(self.source, node.func) or ast.unparse(node.func)
        site = dict(id=identity('call', self.path, self.current['key'], node.lineno,
                               node.col_offset, node.end_lineno, node.end_col_offset),
                    path=self.path, scope=self.current['key'], start_line=node.lineno,
                    end_line=node.end_lineno, col_offset=node.col_offset,
                    end_col_offset=node.end_col_offset, expression=expression,
                    name=node.func.id if isinstance(node.func, ast.Name) else None)
        self.calls.append(site)
        names=sorted({n.id for value in [*node.args,*(k.value for k in node.keywords)]
                      for n in ast.walk(value) if isinstance(n,ast.Name) and isinstance(n.ctx,ast.Load)})
        if names:
            self.escapes.append(dict(call_id=site['id'],scope=self.current['key'],names=names))
        if isinstance(node.func, ast.Name) and node.func.id in {'exec','eval','globals','locals','setattr','delattr'}:
            self.current['dynamic'] = True
        self.generic_visit(node)

    def visit_Name(self, node):
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.bind(node.id, 'write')

    def assignment(self,node,targets,operation='assign'):
        for target in targets:
            parts=_expression_path(target)
            if not parts or len(parts)>2:
                continue
            value=getattr(node,'value',None)
            expr=value.func if isinstance(value,ast.Call) else value
            # Chained targets share one object; this closed IR does not model
            # that alias group, so do not treat them as independent constructors.
            path=_expression_path(expr) if len(targets)==1 else []
            self.assignments.append(dict(
                id=identity('flow',self.path,self.current['key'],type(node).__name__,node.lineno,node.col_offset,parts),
                scope=self.current['key'],start_line=node.lineno,end_line=node.end_lineno,
                col_offset=node.col_offset,end_col_offset=node.end_col_offset,target=parts,
                value=dict(kind=('call' if isinstance(value,ast.Call) else 'name') if path and operation=='assign' else 'unknown',parts=path if operation=='assign' else []),
                conditional=bool(self.conditional),operation=operation,
                statement=type(node).__name__,
                mutation_id=self.mutation_id(target) if isinstance(target,ast.Attribute) else None))
        self.generic_visit(node)

    def visit_Assign(self,node):self.assignment(node,node.targets)
    def visit_AnnAssign(self,node):self.assignment(node,[node.target])
    def visit_AugAssign(self,node):self.assignment(node,[node.target],'invalidate')
    def visit_Delete(self,node):self.assignment(node,node.targets,'invalidate')

    def mutation_id(self,node):
        return identity('mutation',self.path,self.current['key'],node.lineno,node.col_offset,node.end_lineno,node.end_col_offset,node.attr)

    def visit_Import(self, node):
        self.import_declaration(node, None, 0)

    def visit_ImportFrom(self, node):
        self.import_declaration(node, node.module, node.level)

    def import_declaration(self, node, module, level):
        self.imports.append(dict(id=identity('import', self.path, self.current['key'], node.lineno, node.col_offset),
                                 path=self.path, scope=self.current['key'], module=module, level=level,
                                 names=[dict(name=a.name, asname=a.asname) for a in node.names],
                                 start_line=node.lineno, end_line=node.end_lineno,
                                 col_offset=node.col_offset, end_col_offset=node.end_col_offset,
                                 conditional=bool(self.conditional)))
        for alias in node.names:
            self.bind(alias.asname or (alias.name.split('.')[0] if isinstance(node, ast.Import) else alias.name),
                      'import', self.imports[-1]['id'])
            if alias.name == '*':
                self.current['dynamic'] = True

    def conditional_block(self, node):
        self.conditional += 1
        self.generic_visit(node)
        self.conditional -= 1

    visit_If = conditional_block
    visit_For = conditional_block
    visit_AsyncFor = conditional_block
    visit_While = conditional_block
    visit_Try = conditional_block
    visit_TryStar = conditional_block
    visit_Match = conditional_block

    def visit_ExceptHandler(self, node):
        if node.name:
            self.bind(node.name, 'write')
        self.generic_visit(node)

    def visit_MatchAs(self, node):
        if node.name:
            self.bind(node.name, 'write')
        self.generic_visit(node)

    visit_MatchStar = visit_MatchAs

    def visit_MatchMapping(self, node):
        if node.rest:
            self.bind(node.rest, 'write')
        self.generic_visit(node)

    def visit_Global(self, node):
        for name in node.names:
            self.bind(name, 'global_or_nonlocal')

    visit_Nonlocal = visit_Global

    def visit_Attribute(self, node):
        if isinstance(node.ctx,(ast.Store,ast.Del)):
            self.mutations.append(dict(id=self.mutation_id(node),scope=self.current['key'],attribute=node.attr,
                receiver=_expression_path(node.value),start_line=node.lineno,end_line=node.end_lineno,
                col_offset=node.col_offset,end_col_offset=node.end_col_offset))
        if node.attr in {'__globals__','__dict__'} or (isinstance(node.ctx,(ast.Store,ast.Del)) and
            node.attr in {'__code__','__defaults__','__kwdefaults__','__call__'}):
            self.current['dynamic'] = True
        self.generic_visit(node)

    def visit_NamedExpr(self, node):
        owner=self.current
        while owner['kind']=='opaque' and owner.get('opaque_kind')!='Lambda':
            owner=self.by_key[owner['parent']]
        previous=self.current;self.current=owner
        self.bind(node.target.id,'write')
        self.current=previous
        self.visit(node.value)

    def opaque(self, node):
        parent = self.current
        key = f"{parent['key']}/@{type(node).__name__}[{node.lineno}:{node.col_offset}]"
        self.current = self.scope(key, parent['key'], 'opaque', parent['name'], node.lineno, node.end_lineno)
        self.current['opaque_kind'] = type(node).__name__
        self.generic_visit(node)
        self.current = parent

    visit_Lambda = opaque
    visit_ListComp = opaque
    visit_SetComp = opaque
    visit_DictComp = opaque
    visit_GeneratorExp = opaque

    def visit_TypeAlias(self, node):
        # PEP 695 values are lazy and may use alias-local type parameters.
        # Record calls without pretending they resolve in the module namespace.
        self.visit(node.name)
        self.opaque(node)

    def finish(self):
        counts = Counter((d['kind'], d['name']) for d in self.definitions)
        for definition in self.definitions:
            prefix = {'function':'func','method':'method','class':'class'}[definition['kind']]
            base = f"{prefix}:{self.path}:{definition['name']}"
            # Keep unambiguous legacy addresses. Repeated definitions receive
            # location+lexical-key suffixes; none is silently merged/selected.
            definition['id'] = base if counts[definition['kind'],definition['name']] == 1 else (
                base + '@' + identity('def', definition['key'], definition['start_line'], definition['end_line']).split(':')[1])
        return dict(path=self.path, source_sha256=hashlib.sha256(self.source.encode('utf-8')).hexdigest(),
                    line_count=self.scopes[0]['end_line'], scopes=self.scopes,
                    definitions=self.definitions, calls=self.calls, imports=self.imports,
                    assignments=self.assignments,mutations=self.mutations,methods=self.methods,escapes=self.escapes,
                    dynamic=any(s['dynamic'] for s in self.scopes),
                    blocked_names=sorted({name for s in self.scopes for name, values in s['bindings'].items()
                                          if any(b['kind'] in {'global_or_nonlocal','type_parameter'} for b in values)}))


def _expression_path(node):
    if isinstance(node,ast.Name):return [node.id]
    if isinstance(node,ast.Attribute):
        parent=_expression_path(node.value)
        return [*parent,node.attr] if parent and len(parent)<32 else []
    return []


def _simple_init(body,receiver,parameters):
    """Closed initializer grammar, not an execution or general flow analysis."""
    for statement in body:
        if isinstance(statement,ast.Pass) or (isinstance(statement,ast.Expr) and
                isinstance(statement.value,ast.Constant) and isinstance(statement.value.value,str)):
            continue
        if not isinstance(statement,(ast.Assign,ast.AnnAssign)):
            return False
        targets=statement.targets if isinstance(statement,ast.Assign) else [statement.target]
        if len(targets)!=1:
            return False
        target=_expression_path(targets[0])
        if not target or not (len(target)==1 or (len(target)==2 and target[0]==receiver)):
            return False
        value=statement.value
        if isinstance(value,ast.Call):
            if not _expression_path(value.func):return False
            for arg in [*value.args,*(k.value for k in value.keywords)]:
                if not (isinstance(arg,ast.Constant) or isinstance(arg,ast.Name) and arg.id in parameters-{receiver}):
                    return False
        elif value is not None and not isinstance(value,(ast.Constant,ast.Name,ast.Attribute)):
            return False
    return True
