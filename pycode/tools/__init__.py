from pycode.tools.base import ToolContext, ToolResult, ToolSpec
from pycode.tools.git_tools import get_changed_files, get_git_diff
from pycode.tools.query_graph import query_code_graph
from pycode.tools.read_file import read_file
from pycode.tools.retrieve_context import retrieve_context
from pycode.tools.search_code import search_code
from pycode.tools.test_runner import run_pytest

TOOLS = {
    "read_file": ToolSpec("read_file", read_file, read_only=True),
    "search_code": ToolSpec("search_code", search_code, read_only=True),
    "retrieve_context": ToolSpec("retrieve_context", retrieve_context, read_only=True),
    "query_graph": ToolSpec("query_graph", query_code_graph, read_only=True),
    "git_diff": ToolSpec("git_diff", get_git_diff, read_only=True),
    "changed_files": ToolSpec("changed_files", get_changed_files, read_only=True),
    "run_tests": ToolSpec("run_tests", run_pytest, read_only=False),
}

__all__ = [
    "TOOLS",
    "ToolContext",
    "ToolResult",
    "ToolSpec",
    "get_changed_files",
    "get_git_diff",
    "query_code_graph",
    "read_file",
    "retrieve_context",
    "run_pytest",
    "search_code",
]
