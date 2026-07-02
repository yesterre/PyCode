import subprocess
from pathlib import Path

from pycode.tools.base import ToolContext, failure, success, truncate_text


def get_git_diff(
    context: ToolContext,
    *,
    staged: bool = False,
    path: str | Path | None = None,
    max_chars: int | None = None,
):
    """Return read-only git diff output for the project or one project-local path."""
    args = ["git", "diff"]
    if staged:
        args.append("--cached")
    if path is not None:
        try:
            relative = context.relative_path(path)
        except PermissionError as exc:
            return failure("git_diff", "Git diff path denied.", str(exc))
        args.extend(["--", relative])

    completed = _run_git(context, args)
    if isinstance(completed, str):
        return failure("git_diff", "Git diff failed.", completed, command=args)

    output = completed.stdout
    limit = context.max_output_chars if max_chars is None else max_chars
    output, truncated = truncate_text(output, limit)
    return success(
        "git_diff",
        "Git diff collected." if output else "No git diff output.",
        command=args,
        exit_code=completed.returncode,
        diff=output,
        truncated=truncated,
    )


def get_changed_files(context: ToolContext, *, staged: bool = False):
    """Return files changed in git diff without reading file contents."""
    args = ["git", "diff", "--name-only"]
    if staged:
        args.append("--cached")

    completed = _run_git(context, args)
    if isinstance(completed, str):
        return failure("changed_files", "Changed file lookup failed.", completed, command=args)

    files = [line for line in completed.stdout.splitlines() if line.strip()]
    return success(
        "changed_files",
        f"Found {len(files)} changed files.",
        command=args,
        exit_code=completed.returncode,
        files=files,
    )


def _run_git(context: ToolContext, args: list[str]):
    try:
        completed = subprocess.run(
            args,
            cwd=context.project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return str(exc)

    if completed.returncode != 0:
        return completed.stderr.strip() or completed.stdout.strip() or "git command failed"
    return completed
