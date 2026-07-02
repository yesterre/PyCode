from pathlib import Path

from pycode.agent import AgentStep, AgentTask, build_agent_summary_prompt
from pycode.tools import ToolResult


def test_build_agent_summary_prompt_contains_task_steps_and_tool_data() -> None:
    task = AgentTask("分析当前改动", Path("demo_project"), allow_tests=False)
    steps = [
        AgentStep("changed_files", reason="Find changed files."),
        AgentStep("git_diff", reason="Read diff."),
    ]
    results = [
        ToolResult(
            tool="changed_files",
            ok=True,
            summary="Found 1 changed files.",
            data={"files": ["main.py"]},
        ),
        ToolResult(
            tool="git_diff",
            ok=False,
            summary="Git diff failed.",
            error="not a git repository",
        ),
    ]

    prompt = build_agent_summary_prompt(task, steps, results)

    assert "You are the PyCode project-understanding Agent." in prompt
    assert "User task: 分析当前改动" in prompt
    assert "## Step 1: changed_files" in prompt
    assert '"files": [' in prompt
    assert "not a git repository" in prompt
    assert "Do not claim code was modified" in prompt
