from pathlib import Path

import pytest

from pycode.cli import (
    agent_project,
    ask_project,
    build_parser,
    explain_project_target,
    graph_project,
    impact_project_target,
    index_project,
    main,
    memory_project,
    onboard_project,
    query_project_graph,
    task_project,
)
from pycode.models import GraphEdge, GraphNode
from pycode.tools import ToolContext, ToolSpec
from pycode.tools.base import success


def test_graph_project_builds_and_saves_code_graph(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = _create_sample_project(tmp_path)
    output_path = project_path / ".pclens" / "code_graph.json"

    graph = graph_project(project_path)

    captured = capsys.readouterr()
    assert output_path.exists()
    assert "PyCode graph completed." in captured.out
    assert "Graph file:" in captured.out
    assert GraphNode(
        id="file:main.py",
        type="file",
        name="main.py",
        path="main.py",
    ) in graph.nodes
    assert GraphEdge(
        source="file:main.py",
        target="file:services/user_service.py",
        type="imports",
    ) in graph.edges


def test_query_project_graph_reads_graph_and_returns_imports(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = _create_sample_project(tmp_path)
    graph_project(project_path)
    capsys.readouterr()

    result = query_project_graph(project_path, "imports", "main.py")

    captured = capsys.readouterr()
    assert result == [
        GraphEdge(
            source="file:main.py",
            target="file:services/user_service.py",
            type="imports",
        )
    ]
    assert "PyCode query completed." in captured.out
    assert "file:main.py --imports--> file:services/user_service.py" in captured.out


def test_query_project_graph_returns_entry_candidates_without_target(
    tmp_path: Path,
) -> None:
    project_path = _create_sample_project(tmp_path)
    graph_project(project_path)

    result = query_project_graph(project_path, "entry")

    assert result == [
        GraphNode(
            id="file:main.py",
            type="file",
            name="main.py",
            path="main.py",
        )
    ]


def test_query_project_graph_requires_target_for_targeted_queries(
    tmp_path: Path,
) -> None:
    project_path = _create_sample_project(tmp_path)
    graph_project(project_path)

    with pytest.raises(ValueError, match="requires a target"):
        query_project_graph(project_path, "imports")


def test_build_parser_accepts_stage_two_commands() -> None:
    parser = build_parser()

    graph_args = parser.parse_args(["graph", "demo_project", "-o", "graph.json", "--plain"])
    query_args = parser.parse_args(
        ["query", "calls", "demo_project", "func:main.py:main", "--plain"]
    )

    assert graph_args.command == "graph"
    assert graph_args.project_path == Path("demo_project")
    assert graph_args.output_path == Path("graph.json")
    assert graph_args.plain is True
    assert query_args.command == "query"
    assert query_args.query_type == "calls"
    assert query_args.target == "func:main.py:main"
    assert query_args.plain is True


def test_stage_three_commands_use_mock_llm_and_print_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = _create_sample_project(tmp_path)
    index_project(project_path)
    graph_project(project_path)
    capsys.readouterr()
    llm = _MockLLM()

    answer = ask_project(project_path, "这个项目的入口在哪里？", llm_client=llm)

    captured = capsys.readouterr()
    assert answer == "mock answer"
    assert "PyCode answer completed." in captured.out
    assert "Evidence:" in captured.out
    assert "- main.py" in captured.out
    assert "用户问题: 这个项目的入口在哪里？" in llm.prompts[-1]


def test_stage_three_explain_onboard_and_impact_use_mock_llm(
    tmp_path: Path,
) -> None:
    project_path = _create_sample_project(tmp_path)
    index_project(project_path)
    graph_project(project_path)
    llm = _MockLLM()

    explain_project_target(project_path, "services/user_service.py", llm_client=llm)
    onboard_project(project_path, llm_client=llm)
    impact_project_target(project_path, "services/user_service.py", llm_client=llm)

    assert len(llm.prompts) == 3
    assert "问题类型: explain" in llm.prompts[0]
    assert "问题类型: onboard" in llm.prompts[1]
    assert "问题类型: impact" in llm.prompts[2]


def test_stage_three_commands_require_generated_artifacts(tmp_path: Path) -> None:
    project_path = _create_sample_project(tmp_path)

    with pytest.raises(FileNotFoundError, match="Run `pycode index"):
        ask_project(project_path, "入口在哪里？", llm_client=_MockLLM())


def test_main_prints_friendly_runtime_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = _create_sample_project(tmp_path)
    monkeypatch.setattr(
        "sys.argv",
        ["pycode", "ask", str(project_path), "entry?"],
    )

    with pytest.raises(SystemExit) as exc_info:
        main()

    captured = capsys.readouterr()
    assert exc_info.value.code == 1
    assert "Error: Missing PyCode artifacts:" in captured.err


def test_llm_answer_print_handles_unencodable_console_text(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = _create_sample_project(tmp_path)
    index_project(project_path)
    graph_project(project_path)
    capsys.readouterr()

    answer = ask_project(project_path, "入口在哪里？", llm_client=_MockLLM("uses nonbreaking hyphen ‑"))

    captured = capsys.readouterr()
    assert answer == "uses nonbreaking hyphen ‑"
    assert "PyCode answer completed." in captured.out


def test_build_parser_accepts_stage_three_commands() -> None:
    parser = build_parser()

    ask_args = parser.parse_args(["ask", "demo_project", "入口在哪里？", "--plain"])
    explain_args = parser.parse_args(
        ["explain", "demo_project", "main.py", "--model", "gpt-5.5"]
    )
    onboard_args = parser.parse_args(["onboard", "demo_project"])
    impact_args = parser.parse_args(["impact", "demo_project", "main.py"])

    assert ask_args.command == "ask"
    assert ask_args.project_path == Path("demo_project")
    assert ask_args.question == "入口在哪里？"
    assert ask_args.plain is True
    assert explain_args.command == "explain"
    assert explain_args.file_path == "main.py"
    assert explain_args.model == "gpt-5.5"
    assert onboard_args.command == "onboard"
    assert impact_args.command == "impact"
    assert impact_args.file_path == "main.py"


def test_build_parser_accepts_stage_four_agent_command() -> None:
    parser = build_parser()

    agent_args = parser.parse_args(
        [
            "agent",
            "demo_project",
            "分析当前 git diff",
            "--run-tests",
            "--graph",
            "graph.json",
            "--model",
            "gpt-5.5",
            "--plan-only",
            "--no-memory",
            "--no-memory-extract",
            "--show-context",
            "--rule-plan",
            "--plain",
        ]
    )

    assert agent_args.command == "agent"
    assert agent_args.project_path == Path("demo_project")
    assert agent_args.task == "分析当前 git diff"
    assert agent_args.run_tests is True
    assert agent_args.no_tests is False
    assert agent_args.graph_path == Path("graph.json")
    assert agent_args.model == "gpt-5.5"
    assert agent_args.plan_only is True
    assert agent_args.no_memory is True
    assert agent_args.no_memory_extract is True
    assert agent_args.show_context is True
    assert agent_args.rule_plan is True
    assert agent_args.plain is True


def test_build_parser_accepts_v1_demo_commands_and_no_tests_boundary() -> None:
    parser = build_parser()

    query_args = parser.parse_args(["query", "entry", "demo_project"])
    offline_agent_args = parser.parse_args(
        [
            "agent",
            "demo_project",
            "这个项目的入口在哪里？阅读顺序应该是怎样的？",
            "--plan-only",
            "--show-context",
            "--rule-plan",
            "--plain",
        ]
    )
    no_tests_args = parser.parse_args(
        [
            "agent",
            "demo_project",
            "检查 services/user_service.py 的测试覆盖",
            "--no-tests",
            "--show-context",
            "--rule-plan",
        ]
    )
    run_tests_args = parser.parse_args(
        [
            "agent",
            "demo_project",
            "分析当前改动并运行相关测试",
            "--run-tests",
        ]
    )

    assert query_args.command == "query"
    assert query_args.query_type == "entry"
    assert offline_agent_args.plan_only is True
    assert offline_agent_args.show_context is True
    assert offline_agent_args.rule_plan is True
    assert no_tests_args.no_tests is True
    assert no_tests_args.run_tests is False
    assert run_tests_args.run_tests is True
    assert run_tests_args.no_tests is False


def test_build_parser_accepts_stage_five_task_command() -> None:
    parser = build_parser()

    create_args = parser.parse_args(
        [
            "task",
            "demo_project",
            "create",
            "--id",
            "task_001",
            "--title",
            "Build index",
            "--blocked-by",
            "task_000",
            "--owner",
            "codex",
            "--source",
            "manual",
            "--run-id",
            "run-1",
            "--parent-run-id",
            "parent-1",
        ]
    )
    claim_args = parser.parse_args(["task", "demo_project", "claim", "task_001"])

    assert create_args.command == "task"
    assert create_args.project_path == Path("demo_project")
    assert create_args.operation == "create"
    assert create_args.explicit_task_id == "task_001"
    assert create_args.title == "Build index"
    assert create_args.blocked_by == ["task_000"]
    assert create_args.owner == "codex"
    assert create_args.source == "manual"
    assert create_args.run_id == "run-1"
    assert create_args.parent_run_id == "parent-1"
    assert claim_args.operation == "claim"
    assert claim_args.task_id == "task_001"


def test_build_parser_accepts_stage_five_memory_command() -> None:
    parser = build_parser()

    add_args = parser.parse_args(
        [
            "memory",
            "demo_project",
            "add",
            "--id",
            "Project Entry",
            "--type",
            "project",
            "--title",
            "Project Entry",
            "--summary",
            "Entry point",
            "--content",
            "main.py is the entry point.",
            "--tag",
            "entry",
            "--confidence",
            "0.9",
            "--related-file",
            "main.py",
        ]
    )
    load_args = parser.parse_args(["memory", "demo_project", "load", "project-entry"])

    assert add_args.command == "memory"
    assert add_args.project_path == Path("demo_project")
    assert add_args.operation == "add"
    assert add_args.explicit_memory_id == "Project Entry"
    assert add_args.memory_type == "project"
    assert add_args.title == "Project Entry"
    assert add_args.summary == "Entry point"
    assert add_args.tags == ["entry"]
    assert add_args.confidence == 0.9
    assert add_args.related_files == ["main.py"]
    assert load_args.memory_id == "project-entry"


def test_task_project_cli_functions_print_task_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = tmp_path / "demo_project"
    project_path.mkdir()

    task_project(
        project_path,
        "create",
        task_id="task_001",
        title="Build index",
    )
    task_project(
        project_path,
        "create",
        task_id="task_002",
        title="Build graph",
        blocked_by=["task_001"],
    )
    task_project(project_path, "claim", task_id="task_001", owner="codex")
    task_project(project_path, "complete", task_id="task_001")
    task_project(project_path, "list")

    captured = capsys.readouterr()
    assert "PyCode Task created." in captured.out
    assert "- task_001: completed - Build index" in captured.out
    assert "- task_002: pending - Build graph" in captured.out
    assert "Ready tasks: 1" in captured.out
    assert "blocked_by=task_001" in captured.out
    assert "can_start=True" in captured.out


def test_memory_project_cli_functions_print_memory_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = tmp_path / "demo_project"
    project_path.mkdir()

    memory_project(
        project_path,
        "add",
        memory_id="Project Entry",
        memory_type="project",
        title="Project Entry",
        summary="Entry point",
        body="main.py is the entry point.",
        tags=["entry"],
        confidence=0.9,
        related_files=["main.py"],
    )
    memory_project(project_path, "list")
    memory_project(project_path, "search", query="entry")
    memory_project(project_path, "load", memory_id="project-entry")

    captured = capsys.readouterr()
    assert "PyCode Memory created." in captured.out
    assert "PyCode Memory list." in captured.out
    assert "- project-entry: project - Project Entry: Entry point" in captured.out
    assert "main.py is the entry point." in captured.out


def test_agent_project_uses_mock_llm_and_prints_steps(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = tmp_path / "demo_project"
    project_path.mkdir()
    llm = _MockLLM("agent mock answer")

    result = agent_project(
        project_path,
        "分析当前 git diff",
        llm_client=llm,
        tools={
            "changed_files": ToolSpec(
                "changed_files",
                _fake_changed_files,
                read_only=True,
            ),
            "git_diff": ToolSpec("git_diff", _fake_git_diff, read_only=True),
            "retrieve_context": ToolSpec("retrieve_context", _fake_retrieve_context, True),
        },
    )

    captured = capsys.readouterr()
    assert result.answer == "agent mock answer"
    assert "PyCode agent completed." in captured.out
    assert "Task type: diff-impact" in captured.out
    assert "1. changed_files: ok - Found 1 changed files." in captured.out
    assert "2. git_diff: ok - Git diff collected." in captured.out
    assert "Trace:" in captured.out
    assert "tools=3" in captured.out
    assert "denied=0" in captured.out
    assert "Todos:" in captured.out
    assert "completed=3" in captured.out
    assert "failed=0" in captured.out
    assert "Evidence:" in captured.out
    assert "- main.py" in captured.out
    assert "agent mock answer" in captured.out
    assert "User task: 分析当前 git diff" in llm.prompts[-1]


def test_agent_project_plan_only_does_not_call_llm_or_tools(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = tmp_path / "demo_project"
    project_path.mkdir()
    llm = _MockLLM("should not be used")

    result = agent_project(
        project_path,
        "检查 services/user_service.py 的改动影响",
        plan_only=True,
        rule_plan=True,
        llm_client=llm,
        tools={
            "changed_files": ToolSpec(
                "changed_files",
                _raising_tool,
                read_only=True,
            ),
        },
    )

    captured = capsys.readouterr()
    assert result.answer is None
    assert result.tool_results == []
    assert llm.prompts == []
    assert result.planner_source == "rule"
    assert "1. changed_files: planned" in captured.out
    assert "Todos:" in captured.out
    assert "pending=6" in captured.out
    assert "Answer:\n(plan only, no LLM summary generated.)" in captured.out


def test_agent_project_can_route_retrieve_context_through_cli_layer(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = tmp_path / "demo_project"
    project_path.mkdir()
    llm = _MockLLM("agent impact answer")

    result = agent_project(
        project_path,
        "检查 services/user_service.py 的改动影响",
        llm_client=llm,
        tools={
            "changed_files": ToolSpec("changed_files", _fake_changed_files, True),
            "git_diff": ToolSpec("git_diff", _fake_git_diff, True),
            "read_file": ToolSpec("read_file", _fake_read_file, True),
            "retrieve_context": ToolSpec("retrieve_context", _fake_retrieve_context, True),
            "query_graph": ToolSpec("query_graph", _fake_query_graph, True),
        },
    )

    captured = capsys.readouterr()
    assert result.answer == "agent impact answer"
    assert "retrieve_context: ok - Selected 1 context items." in captured.out
    assert "- services/user_service.py" in captured.out
    assert "Selected 1 context items." in llm.prompts[-1]


def test_agent_project_answers_entry_and_onboard_question_through_runtime(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = tmp_path / "demo_project"
    project_path.mkdir()
    llm = _MockLLM("entry and onboard answer")

    result = agent_project(
        project_path,
        "\u8fd9\u4e2a\u9879\u76ee\u7684\u5165\u53e3\u5728\u54ea\uff1f"
        "\u9605\u8bfb\u987a\u5e8f\u5e94\u8be5\u662f\u600e\u6837\u7684\uff1f",
        llm_client=llm,
        tools={
            "retrieve_context": ToolSpec("retrieve_context", _fake_retrieve_context, True),
        },
    )

    captured = capsys.readouterr()
    assert result.answer == "entry and onboard answer"
    assert result.task.task_type == "onboard-question"
    assert [step.arguments["intent"] for step in result.steps] == ["entry", "onboard"]
    assert "Runtime turns: 3" in captured.out
    assert "Runtime:" in captured.out
    assert "Trace:" in captured.out
    assert "tools=2" in captured.out
    assert "Todos:" in captured.out
    assert "completed=2" in captured.out
    assert "retrieve_context: ok - Selected 1 context items." in captured.out
    assert "- services/user_service.py" in captured.out


def test_agent_project_can_show_context_sections(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = tmp_path / "demo_project"
    project_path.mkdir()

    result = agent_project(
        project_path,
        "\u8fd9\u4e2a\u9879\u76ee\u7684\u5165\u53e3\u5728\u54ea\uff1f",
        plan_only=True,
        show_context=True,
        tools={"retrieve_context": ToolSpec("retrieve_context", _raising_tool, True)},
    )

    captured = capsys.readouterr()
    assert result.context is not None
    assert "Context:" in captured.out
    assert "- identity: placement=system" in captured.out
    assert "- plan: placement=user" in captured.out
    assert "No tools were executed." not in captured.out


def _create_sample_project(tmp_path: Path) -> Path:
    project_path = tmp_path / "demo_project"
    service_dir = project_path / "services"
    service_dir.mkdir(parents=True)

    (project_path / "main.py").write_text(
        "\n".join(
            [
                "from services.user_service import UserService",
                "",
                "def main():",
                "    service = UserService()",
                "    service.get_user()",
                "",
                "if __name__ == '__main__':",
                "    main()",
            ]
        ),
        encoding="utf-8",
    )
    (service_dir / "user_service.py").write_text(
        "\n".join(
            [
                "class UserService:",
                "    def get_user(self):",
                "        return 'alice'",
            ]
        ),
        encoding="utf-8",
    )
    return project_path


class _MockLLM:
    def __init__(self, answer: str = "mock answer") -> None:
        self.prompts: list[str] = []
        self.answer = answer

    def generate(self, prompt: str) -> str:
        if "Extract durable PyCode memories" in prompt:
            return "[]"
        self.prompts.append(prompt)
        return self.answer


def _fake_changed_files(context: ToolContext):
    return success("changed_files", "Found 1 changed files.", files=["main.py"])


def _fake_git_diff(context: ToolContext):
    return success("git_diff", "Git diff collected.", diff="diff --git a/main.py")


def _fake_read_file(context: ToolContext, **kwargs):
    return success("read_file", "Read file.", path=kwargs["file_path"], content="class UserService:")


def _fake_retrieve_context(context: ToolContext, **kwargs):
    return success(
        "retrieve_context",
        "Selected 1 context items.",
        evidence=["services/user_service.py", "file:services/user_service.py"],
        items=[
            {
                "path": "services/user_service.py",
                "node_ids": ["file:services/user_service.py"],
                "edges": ["file:main.py --imports--> file:services/user_service.py"],
            }
        ],
    )


def _fake_query_graph(context: ToolContext, **kwargs):
    return success(
        "query_graph",
        "Found 1 imported-by edges.",
        edges=[
            {
                "source": "file:main.py",
                "target": "file:services/user_service.py",
                "type": "imports",
            }
        ],
    )


def _raising_tool(context: ToolContext, **kwargs):
    raise AssertionError("tool should not run in plan-only mode")
