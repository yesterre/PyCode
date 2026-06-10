import ast
from pathlib import Path

from pycode.models import ClassInfo, FileInfo


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

    for node in tree.body:
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = "." * node.level + (node.module or "")
            imports.extend(_format_import_from(module, alias.name) for alias in node.names)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(node.name)
        elif isinstance(node, ast.ClassDef):
            classes.append(_parse_class(node))

    return FileInfo(
        path=display_path,
        imports=imports,
        classes=classes,
        functions=functions,
    )


def _parse_class(node: ast.ClassDef) -> ClassInfo:
    methods = [
        item.name
        for item in node.body
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return ClassInfo(name=node.name, methods=methods)


def _format_import_from(module: str, name: str) -> str:
    if module:
        return f"{module}.{name}"
    return name
