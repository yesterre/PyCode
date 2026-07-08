from pycode.tools.base import ToolContext, ToolResult, ToolSpec
from pycode.tools.git_tools import get_changed_files, get_git_diff
from pycode.tools.memory_tools import memory
from pycode.tools.query_graph import query_code_graph
from pycode.tools.read_file import read_file
from pycode.tools.retrieve_context import retrieve_context
from pycode.tools.search_code import search_code
from pycode.tools.task_tools import task_dag
from pycode.tools.test_runner import run_pytest
from pycode.tools.todo_write import todo_write

TOOLS = {
    "read_file": ToolSpec(
        "read_file",
        read_file,
        read_only=True,
        description=(
            "Read one UTF-8 text file inside the project. Use file_path for the "
            "project-relative path; do not use path."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Project-relative file path to read.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "1-based first line to include.",
                },
                "end_line": {
                    "type": ["integer", "null"],
                    "description": "1-based last line to include. Omit to read to EOF.",
                },
                "max_chars": {
                    "type": ["integer", "null"],
                    "description": "Maximum characters to return.",
                },
            },
            "required": ["file_path"],
        },
        examples=[
            {"file_path": "main.py"},
            {"file_path": "services/user_service.py", "start_line": 1, "end_line": 80},
        ],
    ),
    "search_code": ToolSpec(
        "search_code",
        search_code,
        read_only=True,
        description="Search project-local text files and return path plus line evidence.",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Search text or regex."},
                "include_globs": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "File globs to search, default is ['*.py'].",
                },
                "max_results": {"type": "integer", "description": "Maximum matches."},
                "case_sensitive": {"type": "boolean"},
                "use_regex": {"type": "boolean"},
            },
            "required": ["pattern"],
        },
        examples=[
            {"pattern": "UserService"},
            {"pattern": "def test_", "include_globs": ["tests/*.py", "tests/**/*.py"]},
        ],
    ),
    "retrieve_context": ToolSpec(
        "retrieve_context",
        retrieve_context,
        read_only=True,
        description=(
            "Reuse PyCode retrieval to select relevant code context for a project "
            "question, explanation, onboarding, dependency, or impact task."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "User question or task."},
                "target": {
                    "type": ["string", "null"],
                    "description": "Optional project-relative file target.",
                },
                "intent": {
                    "type": ["string", "null"],
                    "description": "Optional intent such as general, entry, onboard, explain, dependency, impact.",
                },
                "graph_path": {"type": ["string", "null"]},
                "index_path": {"type": ["string", "null"]},
                "max_snippet_chars": {"type": "integer"},
            },
            "required": ["question"],
        },
        examples=[
            {"question": "这个项目的入口在哪里？", "intent": "entry"},
            {"question": "修改用户服务会影响哪里？", "intent": "impact", "target": "services/user_service.py"},
        ],
    ),
    "query_graph": ToolSpec(
        "query_graph",
        query_code_graph,
        read_only=True,
        description="Query the saved code graph for imports, imported-by, calls, or entry candidates.",
        input_schema={
            "type": "object",
            "properties": {
                "query_type": {
                    "type": "string",
                    "enum": ["imports", "imported-by", "calls", "entry"],
                },
                "target": {
                    "type": ["string", "null"],
                    "description": "Required for imports, imported-by, and calls.",
                },
                "graph_path": {"type": ["string", "null"]},
            },
            "required": ["query_type"],
        },
        examples=[
            {"query_type": "entry"},
            {"query_type": "imports", "target": "main.py"},
        ],
    ),
    "git_diff": ToolSpec(
        "git_diff",
        get_git_diff,
        read_only=True,
        description="Read git diff output for the whole project or one project-local path.",
        input_schema={
            "type": "object",
            "properties": {
                "staged": {"type": "boolean", "description": "Use git diff --cached."},
                "path": {"type": ["string", "null"], "description": "Optional project-local path."},
                "max_chars": {"type": ["integer", "null"]},
            },
            "required": [],
        },
        examples=[
            {},
            {"staged": True},
            {"path": "services/user_service.py"},
        ],
    ),
    "changed_files": ToolSpec(
        "changed_files",
        get_changed_files,
        read_only=True,
        description="List files changed in git diff without reading file contents.",
        input_schema={
            "type": "object",
            "properties": {
                "staged": {"type": "boolean", "description": "Use git diff --cached --name-only."},
            },
            "required": [],
        },
        examples=[{}, {"staged": True}],
    ),
    "run_tests": ToolSpec(
        "run_tests",
        run_pytest,
        read_only=False,
        description=(
            "Run pytest through a controlled command. Only use when the Agent task "
            "has allow_tests=True."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "test_paths": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Project-local test paths. Defaults to ['tests'].",
                },
                "extra_args": {
                    "type": ["array", "null"],
                    "items": {"type": "string"},
                    "description": "Extra pytest args.",
                },
                "timeout": {"type": "integer", "description": "Timeout in seconds."},
            },
            "required": [],
        },
        examples=[
            {"test_paths": ["tests"], "extra_args": ["-q"], "timeout": 60},
        ],
    ),
    "todo_write": ToolSpec(
        "todo_write",
        todo_write,
        read_only=False,
        writes_internal_state=True,
        description="Read or update the current in-memory Agent todo list.",
        input_schema={
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["list", "set_status"]},
                "todo_id": {"type": ["string", "null"]},
                "status": {"type": ["string", "null"]},
                "error": {"type": ["string", "null"]},
            },
            "required": [],
        },
        examples=[
            {"operation": "list"},
            {"operation": "set_status", "todo_id": "todo-1", "status": "completed"},
        ],
    ),
    "task_dag": ToolSpec(
        "task_dag",
        task_dag,
        read_only=False,
        writes_internal_state=True,
        description="Manage project-local Task DAG files under .pclens/tasks.",
        input_schema={
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["create", "list", "get", "claim", "complete"]},
                "task_id": {"type": ["string", "null"]},
                "title": {"type": ["string", "null"]},
                "description": {"type": "string"},
                "owner": {"type": ["string", "null"]},
                "blocked_by": {
                    "type": ["array", "string", "null"],
                    "items": {"type": "string"},
                },
                "metadata": {"type": ["object", "null"]},
            },
            "required": ["operation"],
        },
        examples=[
            {"operation": "list"},
            {"operation": "get", "task_id": "task_001"},
        ],
    ),
    "memory": ToolSpec(
        "memory",
        memory,
        read_only=False,
        writes_internal_state=True,
        description="Manage project-local persistent memories under .pclens/memory.",
        input_schema={
            "type": "object",
            "properties": {
                "operation": {"type": "string", "enum": ["add", "list", "search", "load", "rebuild"]},
                "name": {"type": ["string", "null"]},
                "memory_type": {"type": ["string", "null"], "enum": ["user", "feedback", "project", "reference", None]},
                "description": {"type": "string"},
                "body": {"type": ["string", "null"]},
                "tags": {
                    "type": ["array", "string", "null"],
                    "items": {"type": "string"},
                },
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["operation"],
        },
        examples=[
            {"operation": "search", "query": "entry point", "limit": 3},
            {"operation": "list"},
        ],
    ),
}

__all__ = [
    "TOOLS",
    "ToolContext",
    "ToolResult",
    "ToolSpec",
    "get_changed_files",
    "get_git_diff",
    "memory",
    "query_code_graph",
    "read_file",
    "retrieve_context",
    "run_pytest",
    "search_code",
    "task_dag",
    "todo_write",
]
