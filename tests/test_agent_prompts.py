from pathlib import Path

from pycode.agent.memory import MemoryItem, MemoryType
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

    prompt = build_agent_summary_prompt(
        task,
        steps,
        results,
        memory_index="- [project-entry](project-entry.md) - Entry point",
        relevant_memories=[
            MemoryItem(
                name="project-entry",
                type=MemoryType.PROJECT,
                description="Entry point",
                body="main.py is the entry point.",
                path="project-entry.md",
            )
        ],
    )

    assert "You are the PyCode project-understanding Agent." in prompt
    assert "--- Static Context ---" in prompt
    assert "--- Dynamic Context ---" in prompt
    assert "User task: 分析当前改动" in prompt
    assert "## Step 1: changed_files" in prompt
    assert '"files": [' in prompt
    assert "not a git repository" in prompt
    assert "Do not claim code was modified" in prompt
    assert "Project memory index:" in prompt
    assert "<relevant_memories>" in prompt
    assert "main.py is the entry point." in prompt
