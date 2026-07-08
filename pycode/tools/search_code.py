import fnmatch
import re
from pathlib import Path

from pycode.constants import DEFAULT_ARTIFACT_DIR
from pycode.tools.base import ToolContext, failure, success

DEFAULT_EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    DEFAULT_ARTIFACT_DIR,
    ".pytest_tmp",
    ".pytest_cache",
}


def search_code(
    context: ToolContext,
    pattern: str,
    *,
    include_globs: list[str] | None = None,
    max_results: int = 50,
    case_sensitive: bool = False,
    use_regex: bool = False,
):
    """Search project-local text files and return path plus line evidence."""
    if not pattern:
        return failure("search_code", "Search pattern is empty.", "pattern is required")
    if max_results < 1:
        return failure("search_code", "Invalid result limit.", "max_results must be >= 1")

    include_globs = include_globs or ["*.py"]
    matcher = _build_matcher(pattern, case_sensitive, use_regex)
    if isinstance(matcher, str):
        return failure("search_code", "Invalid search pattern.", matcher)

    matches: list[dict[str, object]] = []
    for path in _iter_search_files(context.project_root, include_globs):
        relative = path.relative_to(context.project_root).as_posix()
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except UnicodeDecodeError:
            continue

        for line_number, line in enumerate(lines, start=1):
            if matcher(line):
                matches.append(
                    {
                        "path": relative,
                        "line_number": line_number,
                        "line": line.strip(),
                    }
                )
                if len(matches) >= max_results:
                    return success(
                        "search_code",
                        f"Found {len(matches)} matches for {pattern!r}.",
                        pattern=pattern,
                        matches=matches,
                        truncated=True,
                    )

    return success(
        "search_code",
        f"Found {len(matches)} matches for {pattern!r}.",
        pattern=pattern,
        matches=matches,
        truncated=False,
    )


def _build_matcher(pattern: str, case_sensitive: bool, use_regex: bool):
    if use_regex:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            compiled = re.compile(pattern, flags)
        except re.error as exc:
            return str(exc)
        return lambda line: compiled.search(line) is not None

    needle = pattern if case_sensitive else pattern.lower()
    return lambda line: needle in (line if case_sensitive else line.lower())


def _iter_search_files(project_root: Path, include_globs: list[str]):
    candidates: list[Path] = []
    seen: set[Path] = set()
    normalized_globs = [_normalize_glob(glob) for glob in include_globs]
    for include_glob in normalized_globs:
        for path in project_root.rglob(include_glob):
            if path in seen:
                continue
            seen.add(path)
            candidates.append(path)

    for path in sorted(candidates):
        if not path.is_file():
            continue
        if any(part in DEFAULT_EXCLUDED_DIRS for part in path.relative_to(project_root).parts):
            continue
        relative = path.relative_to(project_root).as_posix()
        if any(fnmatch.fnmatch(relative, glob) for glob in normalized_globs):
            yield path


def _normalize_glob(glob: str) -> str:
    return glob.replace("\\", "/").lstrip("/")
