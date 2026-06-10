import argparse
from pathlib import Path

from pycode.models import ProjectIndex
from pycode.parser import parse_python_file
from pycode.scanner import scan_python_files
from pycode.storage import save_index


DEFAULT_INDEX_DIR = ".pclens"
DEFAULT_INDEX_FILE = "index.json"


def index_project(
    project_path: Path,
    output_path: Path | None = None,
) -> ProjectIndex:
    """Scan a Python project, parse file structures, and save an index file."""
    if output_path is None:
        output_path = project_path / DEFAULT_INDEX_DIR / DEFAULT_INDEX_FILE

    python_files = scan_python_files(project_path)
    file_infos = [
        parse_python_file(file_path, project_path)
        for file_path in python_files
    ]
    project_index = ProjectIndex(
        project_path=str(project_path),
        files=file_infos,
    )

    save_index(project_index, output_path)
    _print_summary(project_index, output_path)
    return project_index


def _print_summary(index: ProjectIndex, output_path: Path) -> None:
    import_count = sum(len(file.imports) for file in index.files)
    class_count = sum(len(file.classes) for file in index.files)
    function_count = sum(len(file.functions) for file in index.files)

    print("PyCode index completed.")
    print(f"Project path: {index.project_path}")
    print(f"Python files: {len(index.files)}")
    print(f"Imports: {import_count}")
    print(f"Classes: {class_count}")
    print(f"Functions: {function_count}")
    print(f"Index file: {output_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pycode",
        description="PyCode: Python code structure indexing tool.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser(
        "index",
        help="Scan a Python project and generate index.json.",
    )
    index_parser.add_argument(
        "project_path",
        type=Path,
        help="Python project directory to index.",
    )
    index_parser.add_argument(
        "--output",
        "-o",
        dest="output_path",
        type=Path,
        default=None,
        help="Path to write the generated index JSON. Defaults to <project>/.pclens/index.json.",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "index":
        index_project(args.project_path, args.output_path)


if __name__ == "__main__":
    main()
