from pathlib import Path

from pycode.agent import (
    AgentStep,
    AgentTask,
    ContextAssembler,
    TaskNode,
    build_agent_summary_context,
)
from pycode.agent.memory import MemoryItem, MemoryType
from pycode.agent.todo import TodoManager
from pycode.tools import ToolResult, ToolSpec


def test_context_assembler_builds_sections_from_runtime_state() -> None:
    task = AgentTask("Analyze current diff", Path("demo_project"), allow_tests=False)
    steps = [AgentStep("changed_files", reason="Find changed files.")]
    todos = TodoManager.from_steps(steps).items
    result = ToolResult(
        tool="changed_files",
        ok=True,
        summary="Found changed files.",
        data={"files": ["main.py"]},
    )
    task_node = TaskNode(id="task_001", title="Review prompt context")

    context = ContextAssembler(
        task,
        steps,
        [result],
        memory_index="- [project-entry](project-entry.md) - Entry point",
        relevant_memories=[
            MemoryItem(
                id="project-entry",
                type=MemoryType.PROJECT,
                title="Project Entry",
                summary="Entry point",
                body="main.py is the entry point.",
                path="project-entry.md",
                confidence=0.9,
                related_files=["main.py"],
            )
        ],
        todos=todos,
        tasks=[task_node],
        tools={"changed_files": ToolSpec("changed_files", lambda context: result)},
    ).assemble()

    assert context.section_names() == [
        "identity",
        "tools",
        "policy",
        "project",
        "response_contract",
        "memory_index",
        "plan",
        "tool_results",
        "evidence",
        "todo",
        "tasks",
        "memory",
    ]
    assert context.sections[0].placement == "system"
    assert context.sections[-1].placement == "user"
    assert "main.py" in context.to_dict()["sections"][7]["content"]
    assert context.sections[0].included is True
    assert context.sections[0].size_estimate > 0
    assert context.skipped_sections


def test_context_render_key_is_deterministic() -> None:
    task = AgentTask("Explain entry point", Path("demo_project"))
    steps = [AgentStep("query_graph", reason="Find entry.")]

    first = build_agent_summary_context(
        task,
        steps,
        [],
        memory_index="- [project-entry](project-entry.md) - Entry point",
        load_tasks=False,
    )
    second = build_agent_summary_context(
        task,
        steps,
        [],
        memory_index="- [project-entry](project-entry.md) - Entry point",
        load_tasks=False,
    )

    assert first.render_key() == second.render_key()
    assert "memory_index" in first.section_names()
