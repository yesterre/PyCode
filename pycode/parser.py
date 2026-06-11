import ast
from pathlib import Path

from pycode.models import CallInfo, ClassInfo, FileInfo


def parse_python_file(file_path: Path, project_path: Path | None = None) -> FileInfo:
    """Parse a Python file and return its basic structure information."""
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Python file does not exist: {path}")
    if not path.is_file():
        raise IsADirectoryError(f"Python file path is not a file: {path}")
    if path.suffix != ".py":
        raise ValueError(f"Expected a .py file: {path}")

    source = path.read_text(encoding="utf-8-sig")
    tree = ast.parse(source, filename=str(path))

    if project_path is None:
        display_path = path.name
    else:
        display_path = str(path.relative_to(Path(project_path)))

    imports: list[str] = []
    classes: list[ClassInfo] = []
    functions: list[str] = []
    call_infos: list[CallInfo] = []
    has_main_guard = False

    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            imports.extend(_format_import_from(module, alias.name) for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
            _append_call_info(call_infos, _parse_calls(caller=node.name, node=node))
        elif isinstance(node, ast.ClassDef):
            classes.append(_parse_class(node))
            for call_info in _parse_method_calls(node):
                _append_call_info(call_infos, call_info)
        elif isinstance(node, ast.If):
            has_main_guard = has_main_guard or _is_main_guard(node)

    return FileInfo(
        path=display_path,
        imports=imports,
        classes=classes,
        functions=functions,
        call_infos=call_infos,
        has_main_guard=has_main_guard,
    )


def _parse_class(node: ast.ClassDef) -> ClassInfo:
    methods = [
        item.name
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return ClassInfo(name=node.name, methods=methods)


def _parse_method_calls(node: ast.ClassDef) -> list[CallInfo]:
    return [
        _parse_calls(caller=f"{node.name}.{item.name}", node=item)
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def _append_call_info(call_infos: list[CallInfo], call_info: CallInfo) -> None:
    if call_info.calls:
        call_infos.append(call_info)


def _parse_calls(caller: str, node: ast.FunctionDef | ast.AsyncFunctionDef) -> CallInfo:
    collector = _CallCollector()
    collector.visit_function_body(node)
    return CallInfo(caller=caller, calls=collector.calls)


def _format_import_from(module: str, name: str) -> str:
    if module:
        return f"{module}.{name}"
    return name


def _is_main_guard(node: ast.If) -> bool:
    return _is_name_main_compare(node.test)


def _is_name_main_compare(node: ast.AST) -> bool:
    if not isinstance(node, ast.Compare):
        return False
    if len(node.ops) != 1 or len(node.comparators) != 1:
        return False
    if not isinstance(node.ops[0], ast.Eq):
        return False

    left = _literal_or_name(node.left)
    right = _literal_or_name(node.comparators[0])
    return {left, right} == {"__name__", "__main__"}


def _literal_or_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


class _CallCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def visit_function_body(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        for item in node.body:
            self.visit(item)

    def visit_Call(self, node: ast.Call) -> None:
        call_name = _format_call_name(node.func)
        if call_name is not None and call_name not in self.calls:
            self.calls.append(call_name)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        return None

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        return None

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        return None


def _format_call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _format_call_name(node.value)
        if parent:
            return f"{parent}.{node.attr}"
        return node.attr
    return None
