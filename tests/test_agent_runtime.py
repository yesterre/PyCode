from pathlib import Path

from pycode.agent import (
    AgentTask,
    RuntimeConfig,
    execute_tool_call,
    run_agent_runtime,
)
from pycode.agent.memory import MemoryStore
from pycode.agent.types import AgentStopReason, ToolCall
from pycode.tools import ToolContext, ToolSpec
from pycode.tools.base import failure, success


def test_runtime_executes_one_tool_per_turn_and_records_messages() -> None:
    calls: list[dict] = []

    def retrieve_context(context: ToolContext, **kwargs):
        calls.append(kwargs)
        return success("retrieve_context", "Selected context.", evidence=["main.py"])

    task = AgentTask(
        "\u8fd9\u4e2a\u9879\u76ee\u7684\u5165\u53e3\u5728\u54ea\uff1f",
        Path("."),
    )
    result = run_agent_runtime(
        task,
        RuntimeConfig(max_turns=4),
        tools={"retrieve_context": ToolSpec("retrieve_context", retrieve_context, True)},
    )

    assert result.stop_reason == AgentStopReason.FINAL
    assert [turn.tool_call.name for turn in result.turns] == ["retrieve_context"]
    assert calls[0]["intent"] == "entry"
    assert [message.role for message in result.messages] == ["user", "assistant", "tool"]
    assert result.tool_results[0].data["evidence"] == ["main.py"]
    assert result.trace is not None
    assert result.trace.tools[0].tool == "retrieve_context"
    assert result.trace.tools[0].status == "ok"
    assert [(todo.id, todo.status) for todo in result.todos] == [
        ("todo-1", "completed")
    ]
    assert result.steps[0].todo_id == "todo-1"
    assert "TodoStatusChanged" in [
        event.event_type for event in result.trace.events
    ]


def test_runtime_plan_only_skips_tools_and_llm() -> None:
    def raising_tool(context: ToolContext, **kwargs):
        raise AssertionError("tool should not run")

    task = AgentTask(
        "\u8fd9\u4e2a\u9879\u76ee\u7684\u9605\u8bfb\u987a\u5e8f\u662f\u4ec0\u4e48\uff1f",
        Path("."),
    )
    result = run_agent_runtime(
        task,
        RuntimeConfig(plan_only=True, use_llm_planner=False),
        tools={"retrieve_context": ToolSpec("retrieve_context", raising_tool, True)},
        llm_client=_MockLLM("should not be called"),
    )

    assert result.stop_reason == AgentStopReason.PLAN_ONLY
    assert result.tool_results == []
    assert result.turns == []
    assert [step.tool for step in result.steps] == ["retrieve_context"]
    assert result.trace is not None
    assert result.trace.tools == []
    assert result.trace.stop_reason == AgentStopReason.PLAN_ONLY
    assert [(todo.id, todo.status) for todo in result.todos] == [
        ("todo-1", "pending")
    ]


def test_runtime_stops_when_max_turns_is_reached() -> None:
    def retrieve_context(context: ToolContext, **kwargs):
        return success("retrieve_context", "Selected context.")

    task = AgentTask(
        "\u8fd9\u4e2a\u9879\u76ee\u7684\u5165\u53e3\u5728\u54ea\uff1f"
        "\u9605\u8bfb\u987a\u5e8f\u5e94\u8be5\u662f\u600e\u6837\u7684\uff1f",
        Path("."),
    )
    result = run_agent_runtime(
        task,
        RuntimeConfig(max_turns=1),
        tools={"retrieve_context": ToolSpec("retrieve_context", retrieve_context, True)},
    )

    assert result.stop_reason == AgentStopReason.MAX_TURNS
    assert len(result.steps) == 2
    assert len(result.turns) == 1
    assert result.trace is not None
    assert result.trace.stop_reason == AgentStopReason.MAX_TURNS
    assert [todo.status for todo in result.todos] == ["completed", "pending"]


def test_runtime_marks_todo_failed_when_tool_fails() -> None:
    def retrieve_context(context: ToolContext, **kwargs):
        return failure("retrieve_context", "Context retrieval failed.", "no index")

    task = AgentTask(
        "\u8fd9\u4e2a\u9879\u76ee\u7684\u5165\u53e3\u5728\u54ea\uff1f",
        Path("."),
    )
    result = run_agent_runtime(
        task,
        RuntimeConfig(max_turns=4),
        tools={"retrieve_context": ToolSpec("retrieve_context", retrieve_context, True)},
    )

    assert result.tool_results[0].ok is False
    assert result.todos[0].status == "failed"
    assert result.todos[0].error == "no index"


def test_runtime_marks_todo_failed_when_policy_denies_tool() -> None:
    def run_tests(context: ToolContext):
        return success("run_tests", "should not run")

    task = AgentTask(
        "\u8fd0\u884c\u6d4b\u8bd5\u5e76\u603b\u7ed3\u5931\u8d25\u539f\u56e0",
        Path("."),
        allow_tests=True,
    )
    result = run_agent_runtime(
        task,
        RuntimeConfig(max_turns=4),
        tools={"run_tests": ToolSpec("run_tests", run_tests, read_only=False)},
        context=ToolContext(Path("."), allow_tests=False),
    )

    assert result.tool_results[-1].ok is False
    assert result.tool_results[-1].data["denied_by"] == "policy"
    assert result.todos[-1].status == "failed"


def test_execute_tool_call_uses_policy_for_non_read_only_tools() -> None:
    def run_tests(context: ToolContext):
        return success("run_tests", "should not run")

    result = execute_tool_call(
        AgentTask("run tests", Path("."), allow_tests=False),
        ToolCall("run_tests"),
        tools={"run_tests": ToolSpec("run_tests", run_tests, read_only=False)},
    )

    assert result.ok is False
    assert result.summary == "Tool execution denied."


def test_runtime_injects_relevant_memories_and_extracts_after_answer(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    store = MemoryStore(project_path)
    store.add_memory(
        name="Project Entry",
        memory_type="project",
        description="入口 main.py",
        body="入口文件是 main.py。",
    )

    def retrieve_context(context: ToolContext, **kwargs):
        return success("retrieve_context", "Selected context.")

    llm = _MemoryAwareLLM()
    result = run_agent_runtime(
        AgentTask("请分析项目入口", project_path),
        RuntimeConfig(max_turns=4),
        tools={"retrieve_context": ToolSpec("retrieve_context", retrieve_context, True)},
        llm_client=llm,
    )

    assert result.answer == "final answer"
    assert result.memory is not None
    assert [item.name for item in result.memory.relevant_memories] == ["project-entry"]
    assert [item.name for item in result.memory.extracted_memories] == ["no-auto-tests"]
    assert result.context is not None
    assert "memory_index" in result.context.section_names()
    assert "relevant_memories" in result.context.section_names()
    assert "Project memory index:" in llm.prompts[2]
    assert "<relevant_memories>" in llm.prompts[2]
    assert (project_path / ".pclens" / "memory" / "no-auto-tests.md").exists()


def test_runtime_no_memory_extract_skips_automatic_write(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()

    def retrieve_context(context: ToolContext, **kwargs):
        return success("retrieve_context", "Selected context.")

    result = run_agent_runtime(
        AgentTask("请分析项目入口", project_path),
        RuntimeConfig(max_turns=4, enable_memory_extraction=False),
        tools={"retrieve_context": ToolSpec("retrieve_context", retrieve_context, True)},
        llm_client=_MemoryAwareLLM(),
    )

    assert result.memory is not None
    assert result.memory.extracted_memories == []
    assert not (project_path / ".pclens" / "memory" / "no-auto-tests.md").exists()


class _MockLLM:
    def __init__(self, answer: str) -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answer


class _MemoryAwareLLM:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if "Select project memories" in prompt:
            return '["project-entry"]'
        if "Extract durable PyCode memories" in prompt:
            return """
            [
              {
                "name": "no-auto-tests",
                "type": "feedback",
                "description": "Do not run tests automatically.",
                "body": "The user wants test commands instead of automatic test execution.",
                "tags": ["tests"]
              }
            ]
            """
        return "final answer"
