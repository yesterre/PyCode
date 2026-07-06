from pycode.models import CodeGraph, FileInfo, GraphEdge, GraphNode, ProjectIndex
from pycode.utils import normalize_path


def build_code_graph(index: ProjectIndex) -> CodeGraph:
    """Build a code graph from a project index."""
    builder = _CodeGraphBuilder(index)
    return builder.build()


class _CodeGraphBuilder:
    def __init__(self, index: ProjectIndex) -> None:
        self.index = index
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
        self._add_structure_nodes()
        self._add_import_edges()
        self._add_call_edges()
        return CodeGraph(
            project_path=self.index.project_path,
            nodes=list(self.nodes.values()),
            edges=self.edges,
        )

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

    def _add_edge(self, source: str, target: str, edge_type: str) -> None:
        key = (source, target, edge_type)
        if key in self.edge_keys:
            return
        self.edge_keys.add(key)
        self.edges.append(GraphEdge(source=source, target=target, type=edge_type))


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
