from pathlib import Path

from pycode.tools.base import ToolContext, failure, success, truncate_text


def read_file(
    context: ToolContext,
    file_path: str | Path,
    *,
    start_line: int = 1,
    end_line: int | None = None,
    max_chars: int | None = None,
):
    """Read a project-local text file with optional line slicing."""
    try:
        path = context.resolve_in_project(file_path)
    except PermissionError as exc:
        return failure("read_file", "File read denied.", str(exc))

    if not path.exists():
        return failure("read_file", "File does not exist.", f"Missing file: {file_path}")
    if not path.is_file():
        return failure("read_file", "Path is not a file.", f"Not a file: {file_path}")
    if start_line < 1:
        return failure("read_file", "Invalid line range.", "start_line must be >= 1")
    if end_line is not None and end_line < start_line:
        return failure(
            "read_file",
            "Invalid line range.",
            "end_line must be greater than or equal to start_line",
        )

    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except UnicodeDecodeError as exc:
        return failure("read_file", "File is not valid UTF-8 text.", str(exc))

    selected = lines[start_line - 1 : end_line]
    content = "\n".join(selected)
    limit = context.max_output_chars if max_chars is None else max_chars
    content, truncated = truncate_text(content, limit)
    relative = path.relative_to(context.project_root).as_posix()

    return success(
        "read_file",
        f"Read {relative}.",
        path=relative,
        start_line=start_line,
        end_line=end_line,
        content=content,
        line_count=len(lines),
        returned_line_count=len(selected),
        truncated=truncated,
    )
