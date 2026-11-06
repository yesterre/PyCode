from pathlib import Path
import errno
import subprocess

from pycode.agent.planner import classify_task, plan_task
from pycode.agent.policy import authorize_tool_call
from pycode.agent.prompts import build_agent_summary_prompt
from pycode.agent.types import AgentResult, AgentStep, AgentTask, RuntimeConfig, ToolCall
from pycode.llm_client import LLMClient
from pycode.tools import TOOLS, ToolContext, ToolResult, ToolSpec
from pycode.tools.base import failure, validate_tool_arguments, normalize_tool_result


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
        return normalize_tool_result(failure(
            tool_call.name,
            "Unknown tool.",
            f"Tool is not registered: {tool_call.name}",
        ), tool_call.name, attempts=0, category='unknown_tool')

    denied = authorize_tool_call(tool_call.name, spec, tool_context)
    if denied is not None:
        return normalize_tool_result(denied,tool_call.name,attempts=0,category='permission_denied')

    argument_error = validate_tool_arguments(spec, tool_call.arguments)
    if argument_error is not None:
        return normalize_tool_result(failure(
            tool_call.name,
            "Tool arguments are invalid.",
            argument_error,
        ),tool_call.name,attempts=0,category='invalid_arguments')

    from pycode.agent.budget import current_budget
    for attempt in range(1,spec.max_attempts+1):
        # Retry uses the same authority and A05a counters, never a new scope.
        denied = authorize_tool_call(tool_call.name,spec,tool_context)
        if denied is not None:
            return normalize_tool_result(denied,tool_call.name,attempts=attempt-1,category='permission_denied')
        if (budget := current_budget()) is not None:
            if tool_context.retrieval_context is not None:
                budget.validate_binding(tool_context.retrieval_context.binding)
            budget.consume('tool_calls')
        category = None
        try:
            result = spec.handler(tool_context, **tool_call.arguments)
        except Exception as exc:
            if isinstance(exc,PermissionError):
                category = 'permission_denied'
            elif isinstance(exc,FileNotFoundError):
                category = 'not_found'
            elif isinstance(exc,(TimeoutError,subprocess.TimeoutExpired)):
                category = 'timeout'
            elif isinstance(exc,OSError) and exc.errno in {errno.EAGAIN,errno.EINTR}:
                category = 'transient_io'
            else:
                category = 'internal_error'
            result = failure(tool_call.name,'Tool execution raised an exception.',f'{type(exc).__name__}: {exc}')
        result = normalize_tool_result(result,tool_call.name,attempts=attempt,category=category)
        retryable = (not result.ok and result.execution.category in spec.retry_on
                     and spec.read_only and not spec.writes_internal_state and not spec.destructive
                     and not spec.requires_confirmation)
        result = normalize_tool_result(result,tool_call.name,attempts=attempt,retryable=retryable)
        if not retryable or attempt == spec.max_attempts:
            return result
        if budget is not None:
            budget.check()


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
    trace_observer=None,
    context: ToolContext | None = None,
) -> AgentResult:
    """Run the Agent runtime loop and optionally ask an LLM to summarize evidence."""
    from pycode.agent.runtime import run_agent_runtime

    seed = context.initial_evidence if context is not None else None
    from pycode.agent.budget import current_budget
    if (budget := current_budget()) is not None:
        max_steps = budget.limits.max_turns
    if seed is not None:
        if context.project_root != Path(project_path).resolve() or context.project_root != seed.context.root:
            raise ValueError('Initial evidence belongs to another workspace')
        seed.validate(context.retrieval_context)

    task = AgentTask(
        description=description,
        initial_evidence=seed,
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
        trace_observer=trace_observer,
        context=context,
    )
