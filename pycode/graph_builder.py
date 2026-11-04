from dataclasses import asdict
from pycode.models import CodeGraph, FileInfo, GraphEdge, GraphNode, ProjectIndex
from pycode.utils import normalize_path


def build_code_graph(index: ProjectIndex, *, binding=None) -> CodeGraph:
    """Build a code graph from a project index."""
    builder = _CodeGraphBuilder(index, binding)
    return builder.build()


class _CodeGraphBuilder:
    def __init__(self, index: ProjectIndex, binding=None) -> None:
        self.index = index
        self.binding = binding
        self.nodes: dict[str, GraphNode] = {}
        self.edges: list[GraphEdge] = []
        self.edge_keys: set[tuple[str, str, str]] = set()
        self.files_by_path = {
            normalize_path(file_info.path): file_info for file_info in index.files
        }
        self.module_to_file = {
            _path_to_module(path): path for path in self.files_by_path
        }
        self.symbol_to_node: dict[str, str] = {}

    def build(self) -> CodeGraph:
        available = [file.structure is not None for file in self.index.files]
        if any(available) and not all(available):
            raise ValueError('Mixed structural Graph capabilities')
        if self.index.structure_version == 2 or (available and all(available)):
            return self._structured()
        self._add_structure_nodes()
        self._add_import_edges()
        self._add_call_edges()
        return CodeGraph(
            project_path=self.index.project_path,
            nodes=list(self.nodes.values()),
            edges=self.edges,
        )

    def _structured(self):
        from pycode.structure import CAPABILITIES, identity, resolve_direct
        modules = {}
        for path in self.files_by_path:
            modules.setdefault(_path_to_module(path), []).append(path)
        self.module_to_file = {name:paths[0] for name,paths in modules.items() if len(paths)==1}
        files, scopes, calls, declarations = [], [], [], []
        for file in sorted(self.index.files, key=lambda f: normalize_path(f.path)):
            facts = file.structure
            if facts is None:
                raise ValueError('Missing structural facts')
            path = normalize_path(file.path)
            if facts['path'] != path:
                raise ValueError('Structure file identity mismatch')
            file_id = _file_id(path)
            scope_ids = {'<module>':file_id, **{d['key']:d['id'] for d in facts['definitions']}}
            for scope in facts['scopes']:
                scope_ids.setdefault(scope['key'], identity('scope', path, scope['key']))
            files.append({k:facts[k] for k in ('path','source_sha256','line_count')})
            self._add_node(GraphNode(file_id, 'file', path, path, symbol_key='<module>'))
            for scope in facts['scopes']:
                scopes.append(dict(id=scope_ids[scope['key']], path=path, key=scope['key'],
                                   parent_id=scope_ids.get(scope['parent']), kind=scope['kind'],
                                   name=scope['name'], start_line=scope['start_line'], end_line=scope['end_line']))
            for d in facts['definitions']:
                parent_id = scope_ids[d['parent']]
                self._add_node(GraphNode(d['id'], d['kind'], d['name'], path,
                                         d['key'], parent_id, d['start_line'], d['end_line']))
                self._add_edge(parent_id, d['id'], 'contains', resolution='static', basis='lexical_containment')
            scope_map = {s['key']:s for s in facts['scopes']}
            definitions = {d['key']:d for d in facts['definitions']}
            for declaration in facts['imports']:
                value = {k:v for k,v in declaration.items() if k!='scope'}
                value['scope_id'] = scope_ids[declaration['scope']]
                declarations.append(value)
                module = '.' * declaration['level'] + (declaration['module'] or '')
                for alias in declaration['names']:
                    name = module + ('' if module.endswith('.') else '.') + alias['name'] if module else alias['name']
                    # Declaration links only. Relative/alias symbol binding is
                    # deferred; never use this lookup to resolve a call.
                    candidates = [] if declaration['level'] else ([name, module] if module else [name])
                    target_path = next((self.module_to_file[c] for c in candidates if c in self.module_to_file), None)
                    target = _file_id(target_path) if target_path else _external_import_id(name)
                    if target_path is None:
                        self._add_node(GraphNode(target, 'external', name))
                    self._add_edge(file_id, target, 'imports', site_id=declaration['id'], path=path,
                                   start_line=declaration['start_line'], end_line=declaration['end_line'],
                                   resolution='declaration', basis='import_declaration_not_symbol_binding')
            for site in facts['calls']:
                status, basis, candidates = resolve_direct(site, facts, scope_map, definitions)
                caller = scope_map[site['scope']]
                while caller['key'] not in definitions and caller['parent'] is not None:
                    caller = scope_map[caller['parent']]
                caller_id = scope_ids[caller['key']]
                target = candidates[0] if status=='static' else None
                value = {k:v for k,v in site.items() if k not in {'scope','name'}}
                value.update(scope_id=scope_ids[site['scope']], caller_id=caller_id, resolution=status,
                             basis=basis, target_id=target, candidate_ids=candidates[:8], candidate_count=len(candidates))
                calls.append(value)
                if target is None:
                    target = 'unresolved:' + site['id']
                    self._add_node(GraphNode(target, 'external', site['name'] or '<dynamic call>'))
                self._add_edge(caller_id, target, 'calls' if status=='static' else 'unresolved_call',
                               site_id=site['id'], path=path, start_line=site['start_line'], end_line=site['end_line'],
                               resolution=status, basis=basis)
        graph = CodeGraph(self.index.project_path, sorted(self.nodes.values(), key=lambda n:n.id),
                          sorted(self.edges, key=lambda e:(e.source,e.target,e.type,e.site_id or '')),
                          schema_version=2, binding=asdict(self.binding) if self.binding is not None else None,
                          capabilities=CAPABILITIES, files=files, scopes=scopes, call_sites=calls,
                          import_declarations=declarations, producer='pycode-a03a-v1')
        from pycode.graph_schema import validate_graph
        validate_graph(graph)
        return graph

    def _add_structure_nodes(self) -> None:
        for file_info in self.index.files:
            file_path = normalize_path(file_info.path)
            file_id = _file_id(file_path)
            self._add_node(
                GraphNode(id=file_id, type="file", name=file_path, path=file_path)
            )

            for function_name in file_info.functions:
                function_id = _function_id(file_path, function_name)
                self._add_node(
                    GraphNode(
                        id=function_id,
                        type="function",
                        name=function_name,
                        path=file_path,
                    )
                )
                self._add_edge(file_id, function_id, "contains")
                self.symbol_to_node[function_name] = function_id
                self.symbol_to_node[f"{_path_to_module(file_path)}.{function_name}"] = (
                    function_id
                )

            for class_info in file_info.classes:
                class_id = _class_id(file_path, class_info.name)
                self._add_node(
                    GraphNode(
                        id=class_id,
                        type="class",
                        name=class_info.name,
                        path=file_path,
                    )
                )
                self._add_edge(file_id, class_id, "contains")
                self.symbol_to_node[class_info.name] = class_id
                self.symbol_to_node[f"{_path_to_module(file_path)}.{class_info.name}"] = (
                    class_id
                )

                for method_name in class_info.methods:
                    method_id = _method_id(file_path, class_info.name, method_name)
                    method_symbol = f"{class_info.name}.{method_name}"
                    self._add_node(
                        GraphNode(
                            id=method_id,
                            type="method",
                            name=method_symbol,
                            path=file_path,
                        )
                    )
                    self._add_edge(class_id, method_id, "contains")
                    self.symbol_to_node[method_symbol] = method_id
                    self.symbol_to_node[
                        f"{_path_to_module(file_path)}.{method_symbol}"
                    ] = method_id

    def _add_import_edges(self) -> None:
        for file_info in self.index.files:
            source_id = _file_id(normalize_path(file_info.path))
            for import_name in file_info.imports:
                target_id = self._resolve_import_target(import_name)
                self._add_edge(source_id, target_id, "imports")

    def _add_call_edges(self) -> None:
        for file_info in self.index.files:
            file_path = normalize_path(file_info.path)
            for call_info in file_info.call_infos:
                source_id = self._resolve_caller(file_path, call_info.caller)
                if source_id is None:
                    continue
                for call_name in call_info.calls:
                    target_id = self._resolve_call_target(call_name)
                    self._add_edge(source_id, target_id, "calls")

    def _resolve_import_target(self, import_name: str) -> str:
        candidates = _import_module_candidates(import_name)
        for module_name in candidates:
            file_path = self.module_to_file.get(module_name)
            if file_path is not None:
                return _file_id(file_path)

        target_id = _external_import_id(import_name)
        self._add_node(
            GraphNode(id=target_id, type="external", name=import_name, path=None)
        )
        return target_id

    def _resolve_caller(self, file_path: str, caller: str) -> str | None:
        if "." in caller:
            class_name, method_name = caller.split(".", 1)
            method_id = _method_id(file_path, class_name, method_name)
            if method_id in self.nodes:
                return method_id
            return None

        function_id = _function_id(file_path, caller)
        if function_id in self.nodes:
            return function_id
        return None

    def _resolve_call_target(self, call_name: str) -> str:
        if call_name in self.symbol_to_node:
            return self.symbol_to_node[call_name]

        short_name = call_name.rsplit(".", 1)[-1]
        if short_name in self.symbol_to_node:
            return self.symbol_to_node[short_name]

        target_id = _external_call_id(call_name)
        self._add_node(
            GraphNode(id=target_id, type="function", name=call_name, path=None)
        )
        return target_id

    def _add_node(self, node: GraphNode) -> None:
        if node.id not in self.nodes:
            self.nodes[node.id] = node

    def _add_edge(self, source: str, target: str, edge_type: str, **metadata) -> None:
        key = (source, target, edge_type, metadata.get('site_id'))
        if key in self.edge_keys:
            return
        self.edge_keys.add(key)
        self.edges.append(GraphEdge(source=source, target=target, type=edge_type, **metadata))


def _path_to_module(path: str) -> str:
    normalized = normalize_path(path)
    if normalized.endswith("/__init__.py"):
        return normalized[: -len("/__init__.py")].replace("/", ".")
    if normalized.endswith(".py"):
        return normalized[:-3].replace("/", ".")
    return normalized.replace("/", ".")


def _import_module_candidates(import_name: str) -> list[str]:
    parts = import_name.split(".")
    return [".".join(parts[:index]) for index in range(len(parts), 0, -1)]


def _file_id(path: str) -> str:
    return f"file:{path}"


def _class_id(path: str, class_name: str) -> str:
    return f"class:{path}:{class_name}"


def _function_id(path: str, function_name: str) -> str:
    return f"func:{path}:{function_name}"


def _method_id(path: str, class_name: str, method_name: str) -> str:
    return f"method:{path}:{class_name}.{method_name}"


def _external_import_id(import_name: str) -> str:
    return f"external:{import_name}"


def _external_call_id(call_name: str) -> str:
    return f"func:{call_name}"
