from pathlib import Path

from pycode.agent import (
    AgentTask,
    HookContext,
    HookEventType,
    HookRegistry,
    HookResult,
    RuntimeConfig,
    run_agent_runtime,
)
from pycode.tools import ToolContext, ToolSpec
from pycode.tools.base import success


def test_hook_registry_triggers_handlers_in_registration_order() -> None:
    calls: list[str] = []
    registry = HookRegistry()
    registry.register(HookEventType.PRE_TOOL_USE, lambda context: calls.append("first"))
    registry.register(HookEventType.PRE_TOOL_USE, lambda context: calls.append("second"))

    registry.trigger(
        HookContext(
            event_type=HookEventType.PRE_TOOL_USE,
            task=AgentTask("demo", Path(".")),
        )
    )

    assert calls == ["first", "second"]


def test_pre_tool_hook_deny_prevents_tool_execution() -> None:
    def deny_tool(context: HookContext) -> HookResult:
        return HookResult.deny("Blocked by test hook.", denied_by="test_hook")

    def should_not_run(context: ToolContext, **kwargs):
        raise AssertionError("tool should not run")

    registry = HookRegistry()
    registry.register(HookEventType.PRE_TOOL_USE, deny_tool)
    task = AgentTask("Where is the entry point?", Path("."))

    result = run_agent_runtime(
        task,
        RuntimeConfig(max_turns=1),
        tools={"retrieve_context": ToolSpec("retrieve_context", should_not_run, True)},
        hook_registry=registry,
    )

    assert result.tool_results[0].ok is False
    assert result.tool_results[0].summary == "Tool execution denied by hook."
    assert result.tool_results[0].data["denied_by"] == "test_hook"
    assert result.trace is not None
    assert result.trace.tools[0].status == "denied"
    assert result.trace.tools[0].denied_by == "test_hook"


def test_hook_errors_are_recorded_and_do_not_stop_runtime() -> None:
    def broken_hook(context: HookContext) -> None:
        raise RuntimeError("boom")

    def retrieve_context(context: ToolContext, **kwargs):
        return success("retrieve_context", "Selected context.", evidence=["main.py"])

    registry = HookRegistry()
    registry.register(HookEventType.PRE_TOOL_USE, broken_hook)
    task = AgentTask("Where is the entry point?", Path("."))

    result = run_agent_runtime(
        task,
        RuntimeConfig(max_turns=1),
        tools={"retrieve_context": ToolSpec("retrieve_context", retrieve_context, True)},
        hook_registry=registry,
    )

    assert result.tool_results[0].ok is True
    assert result.trace is not None
    assert any(event.event_type == "HookError" for event in result.trace.events)
