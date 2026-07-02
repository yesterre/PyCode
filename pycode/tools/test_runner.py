import subprocess
import sys
from pathlib import Path

from pycode.tools.base import ToolContext, failure, success, truncate_text


def run_pytest(
    context: ToolContext,
    test_paths: list[str | Path] | None = None,
    *,
    extra_args: list[str] | None = None,
    timeout: int = 60,
):
    """Run pytest through a controlled command when tests are explicitly allowed."""
    if not context.allow_tests:
        return failure(
            "run_tests",
            "Test execution is not allowed.",
            "Set allow_tests=True before running tests.",
        )
    if timeout < 1:
        return failure("run_tests", "Invalid timeout.", "timeout must be >= 1")

    resolved_test_paths: list[str] = []
    for item in test_paths or ["tests"]:
        try:
            resolved = context.resolve_in_project(item)
        except PermissionError as exc:
            return failure("run_tests", "Test path denied.", str(exc))
        resolved_test_paths.append(resolved.relative_to(context.project_root).as_posix())

    command = [
        context.python_executable or sys.executable,
        "-m",
        "pytest",
        *resolved_test_paths,
        *(extra_args or []),
    ]

    try:
        completed = subprocess.run(
            command,
            cwd=context.project_root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        return failure(
            "run_tests",
            "Pytest timed out.",
            f"Timed out after {timeout} seconds.",
            command=command,
            stdout=stdout,
            stderr=stderr,
            timed_out=True,
        )
    except OSError as exc:
        return failure("run_tests", "Pytest could not start.", str(exc), command=command)

    stdout, stdout_truncated = truncate_text(completed.stdout, context.max_output_chars)
    stderr, stderr_truncated = truncate_text(completed.stderr, context.max_output_chars)
    return success(
        "run_tests",
        "Pytest passed." if completed.returncode == 0 else "Pytest failed.",
        command=command,
        exit_code=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        timed_out=False,
    )
