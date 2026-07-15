from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from pycode.agent import AgentTask, RuntimeConfig, run_agent_runtime
from pycode.agent.types import AgentStopReason
from pycode.cli import agent_project, graph_project, index_project, query_project_graph
from pycode.tools import ToolContext, ToolSpec
from pycode.tools.base import failure, success


DEMO_PROJECT = Path("examples/demo_project")


def test_v1_acceptance_entry_and_onboard_show_trace_todo_evidence_context(
    tmp_path: Path,
) -> None:
    project_path = _prepare_demo_project(tmp_path)

    entries = query_project_graph(project_path, "entry")
    result = agent_project(
        project_path,
        "这个项目的入口在哪里？新手阅读顺序应该怎样？",
        rule_plan=True,
        llm_client=_SummaryLLM("入口在 main.py，阅读顺序从 controller 到 service 再到 model/test。"),
        enable_memory=False,
        enable_memory_extraction=False,
        show_context=True,
    )

    assert [entry.path for entry in entries] == ["main.py"]
    assert result.stop_reason == AgentStopReason.FINAL
    assert result.task.task_type == "onboard-question"
    assert [step.arguments["intent"] for step in result.steps] == ["entry", "onboard"]
    assert all(tool_result.ok for tool_result in result.tool_results)
    assert result.trace is not None
    assert "ContextAssembled" in _trace_event_types(result)
    assert "NextActionDecided" in _trace_event_types(result)
    assert all(todo.status == "completed" for todo in result.todos)
    assert result.context is not None
    assert {"identity", "tools", "policy", "todo"}.issubset(result.context.section_names())
    assert _evidence_mentions(result, "main.py")


def test_v1_acceptance_file_impact_uses_file_graph_and_retrieval_evidence(
    tmp_path: Path,
) -> None:
    project_path = _prepare_demo_project(tmp_path)

    result = agent_project(
        project_path,
        "分析 services/user_service.py 的影响范围",
        rule_plan=True,
        llm_client=_SummaryLLM("修改 user_service.py 主要影响 controller 调用和相关测试。"),
        enable_memory=False,
        enable_memory_extraction=False,
    )

    planned_tools = [step.tool for step in result.steps]
    assert "read_file" in planned_tools
    assert "retrieve_context" in planned_tools
    assert planned_tools.count("query_graph") == 2
    assert all(tool_result.ok for tool_result in result.tool_results)
    assert _evidence_mentions(result, "services/user_service.py")
    assert _evidence_mentions(result, "controllers/user_controller.py")
    assert result.trace is not None
    assert result.trace.summary()["tools"] >= 4


def test_v1_acceptance_git_diff_risk_uses_real_temp_git_repository(
    tmp_path: Path,
) -> None:
    if shutil.which("git") is None:
        pytest.skip("git executable is required for the V1.0 git diff acceptance test")
    project_path = _prepare_demo_project(tmp_path)
    _init_git_repo(project_path)
    service_path = project_path / "services" / "user_service.py"
    service_path.write_text(
        service_path.read_text(encoding="utf-8") + "\n# v1 acceptance risk marker\n",
        encoding="utf-8",
    )

    result = agent_project(
        project_path,
        "当前 git diff 有什么风险？",
        rule_plan=True,
        llm_client=_SummaryLLM("当前 diff 修改了 user_service.py，需要关注调用方和测试。"),
        enable_memory=False,
        enable_memory_extraction=False,
    )

    summaries = [tool_result.summary for tool_result in result.tool_results]
    changed_files = next(result for result in result.tool_results if result.tool == "changed_files")
    git_diff = next(result for result in result.tool_results if result.tool == "git_diff")
    assert "Found 1 changed files." in summaries
    assert changed_files.data["files"] == ["services/user_service.py"]
    assert "v1 acceptance risk marker" in git_diff.data["diff"]
    assert all(tool_result.ok for tool_result in result.tool_results)
    assert _evidence_mentions(result, "services/user_service.py")


def test_v1_acceptance_test_coverage_respects_no_tests_and_run_tests_boundary(
    tmp_path: Path,
) -> None:
    project_path = _prepare_demo_project(tmp_path)

    no_tests_result = agent_project(
        project_path,
        "检查 services/user_service.py 是否有测试覆盖",
        rule_plan=True,
        llm_client=_SummaryLLM("已定位到 user_service 相关测试线索。"),
        enable_memory=False,
        enable_memory_extraction=False,
    )
    run_tests_result = agent_project(
        project_path,
        "检查 services/user_service.py 是否有测试覆盖",
        run_tests=True,
        rule_plan=True,
        llm_client=_SummaryLLM("已定位并运行相关测试。"),
        enable_memory=False,
        enable_memory_extraction=False,
        tools={
            "search_code": ToolSpec("search_code", _fake_test_search, read_only=True),
            "run_tests": ToolSpec("run_tests", _fake_run_tests, read_only=False),
        },
    )

    assert [step.tool for step in no_tests_result.steps].count("search_code") >= 1
    assert all(step.tool != "run_tests" for step in no_tests_result.steps)
    assert [step.tool for step in run_tests_result.steps].count("search_code") >= 1
    assert run_tests_result.steps[-1].tool == "run_tests"
    assert run_tests_result.tool_results[-1].summary == "Pytest passed."
    assert all(result.ok for result in run_tests_result.tool_results)


def test_v1_acceptance_error_recovery_records_tool_failure_policy_denial_and_llm_fallback(
    tmp_path: Path,
) -> None:
    project_path = _prepare_demo_project(tmp_path)
    tool_failure = agent_project(
        project_path,
        "这个项目的入口在哪里？",
        rule_plan=True,
        llm_client=_SummaryLLM("工具失败后总结已有证据。"),
        enable_memory=False,
        enable_memory_extraction=False,
        tools={"retrieve_context": ToolSpec("retrieve_context", _failing_retrieve_context, True)},
    )
    policy_denial = run_agent_runtime(
        AgentTask("检查 services/user_service.py 是否有测试覆盖", project_path, allow_tests=True),
        RuntimeConfig(max_turns=3, use_llm_planner=False, enable_memory=False),
        tools={
            "search_code": ToolSpec("search_code", _fake_test_search, read_only=True),
            "run_tests": ToolSpec("run_tests", _fake_run_tests, read_only=False),
        },
        context=ToolContext(project_path, allow_tests=False),
    )
    llm_fallback = agent_project(
        project_path,
        "这个项目的入口在哪里？",
        llm_client=_SequenceLLM(
            ["not a plan", "not a next action", "still not a next action", "fallback summary"]
        ),
        enable_memory=False,
        enable_memory_extraction=False,
        tools={"retrieve_context": ToolSpec("retrieve_context", _fake_retrieve_context, True)},
    )

    assert tool_failure.tool_results[0].ok is False
    assert tool_failure.observations[0].ok is False
    assert tool_failure.todos[0].status == "failed"
    assert "ObservationRecorded" in _trace_event_types(tool_failure)

    denied_result = next(result for result in policy_denial.tool_results if result.tool == "run_tests")
    assert denied_result.ok is False
    assert denied_result.data["denied_by"] == "policy"
    assert "PolicyDecision" in _trace_event_types(policy_denial)

    assert llm_fallback.planner_source == "fallback"
    assert llm_fallback.planner_error is not None
    assert "LLMNextActionSchemaFailed" in _trace_event_types(llm_fallback)
    assert "LLMNextActionFallback" in _trace_event_types(llm_fallback)


def _prepare_demo_project(tmp_path: Path) -> Path:
    project_path = tmp_path / "demo_project"
    shutil.copytree(
        DEMO_PROJECT,
        project_path,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            ".pclens",
            ".pytest_cache",
            ".pytest_tmp*",
            ".venv",
            ".git",
        ),
    )
    index_project(project_path)
    graph_project(project_path)
    return project_path


def _init_git_repo(project_path: Path) -> None:
    commands = [
        ["git", "init"],
        ["git", "config", "user.email", "v1@example.test"],
        ["git", "config", "user.name", "V1 Acceptance"],
        ["git", "add", "."],
        ["git", "commit", "-m", "initial"],
    ]
    for command in commands:
        subprocess.run(
            command,
            cwd=project_path,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )


def _trace_event_types(result) -> list[str]:
    assert result.trace is not None
    return [event.event_type for event in result.trace.events]


def _evidence_mentions(result, text: str) -> bool:
    haystack = "\n".join(
        [
            *(str(item) for tool_result in result.tool_results for item in tool_result.data.get("evidence", [])),
            *(str(tool_result.data) for tool_result in result.tool_results),
        ]
    )
    return text in haystack


def _fake_test_search(context: ToolContext, **kwargs):
    return success(
        "search_code",
        "Found 1 test reference.",
        matches=[
            {
                "path": "tests/test_user_service.py",
                "line": 1,
                "text": "def test_get_user()",
            }
        ],
        evidence=["tests/test_user_service.py"],
    )


def _fake_run_tests(context: ToolContext, **kwargs):
    return success("run_tests", "Pytest passed.", exit_code=0, stdout="1 passed")


def _fake_retrieve_context(context: ToolContext, **kwargs):
    return success(
        "retrieve_context",
        "Selected context.",
        evidence=["main.py"],
        items=[{"path": "main.py"}],
    )


def _failing_retrieve_context(context: ToolContext, **kwargs):
    return failure("retrieve_context", "Context retrieval failed.", "missing artifacts")


class _SummaryLLM:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answer


class _SequenceLLM:
    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if self.responses:
            return self.responses.pop(0)
        return "[]"
