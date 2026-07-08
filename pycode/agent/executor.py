from pathlib import Path

from pycode.agent.planner import classify_task, plan_task
from pycode.agent.policy import authorize_tool_call
from pycode.agent.prompts import build_agent_summary_prompt
from pycode.agent.types import AgentResult, AgentStep, AgentTask, RuntimeConfig, ToolCall
from pycode.llm_client import LLMClient
from pycode.tools import TOOLS, ToolContext, ToolResult, ToolSpec
from pycode.tools.base import failure, validate_tool_arguments


def execute_tool_call(
    task: AgentTask,
    tool_call: ToolCall,
    *,
    tools: dict[str, ToolSpec] | None = None,
    context: ToolContext | None = None,
) -> ToolResult:
    """Execute one tool call through the reusable Agent permission boundary."""
    tool_registry = tools or TOOLS
    tool_context = context or ToolContext(task.project_path, allow_tests=task.allow_tests)
    spec = tool_registry.get(tool_call.name)
    if spec is None:
        return failure(
            tool_call.name,
            "Unknown tool.",
            f"Tool is not registered: {tool_call.name}",
        )

    denied = authorize_tool_call(tool_call.name, spec, tool_context)
    if denied is not None:
        return denied

    argument_error = validate_tool_arguments(spec, tool_call.arguments)
    if argument_error is not None:
        return failure(
            tool_call.name,
            "Tool arguments are invalid.",
            argument_error,
            arguments=tool_call.arguments,
        )

    try:
        return spec.handler(tool_context, **tool_call.arguments)
    except Exception as exc:  # pragma: no cover - defensive boundary
        return failure(
            tool_call.name,
            "Tool execution raised an exception.",
            f"{type(exc).__name__}: {exc}",
        )


def execute_plan(
    task: AgentTask,
    steps: list[AgentStep],
    *,
    tools: dict[str, ToolSpec] | None = None,
    context: ToolContext | None = None,
) -> list[ToolResult]:
    """Execute planned steps and return one result per step."""
    tool_context = context or ToolContext(task.project_path, allow_tests=task.allow_tests)
    results: list[ToolResult] = []

    for step in steps:
        results.append(
            execute_tool_call(
                task,
                ToolCall.from_step(step),
                tools=tools,
                context=tool_context,
            )
        )

    return results


def run_agent_task(
    description: str,
    project_path: str | Path,
    *,
    allow_tests: bool = False,
    graph_path: str | Path | None = None,
    llm_client: LLMClient | None = None,
    tools: dict[str, ToolSpec] | None = None,
    max_steps: int = 8,
    plan_only: bool = False,
    use_llm_planner: bool = True,
    enable_memory: bool = True,
    enable_memory_extraction: bool = True,
) -> AgentResult:
    """Run the Agent runtime loop and optionally ask an LLM to summarize evidence."""
    from pycode.agent.runtime import run_agent_runtime

    task = AgentTask(
        description=description,
        project_path=Path(project_path),
        allow_tests=allow_tests,
        max_steps=max_steps,
        graph_path=Path(graph_path) if graph_path is not None else None,
        task_type=classify_task(description),
    )
    return run_agent_runtime(
        task,
        RuntimeConfig(
            max_turns=max_steps,
            plan_only=plan_only,
            use_llm_planner=use_llm_planner,
            enable_memory=enable_memory,
            enable_memory_extraction=enable_memory_extraction,
        ),
        tools=tools,
        llm_client=llm_client,
    )
