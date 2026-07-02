from pycode.agent.executor import execute_tool_call
from pycode.agent.planner import plan_task
from pycode.agent.prompts import build_agent_summary_prompt
from pycode.agent.types import (
    AgentMessage,
    AgentResult,
    AgentStopReason,
    AgentTask,
    AgentTurn,
    RuntimeConfig,
    ToolCall,
)
from pycode.llm_client import LLMClient
from pycode.tools import ToolContext, ToolResult, ToolSpec


def run_agent_runtime(
    task: AgentTask,
    config: RuntimeConfig | None = None,
    *,
    tools: dict[str, ToolSpec] | None = None,
    llm_client: LLMClient | None = None,
    context: ToolContext | None = None,
) -> AgentResult:
    """Run a minimal Agent loop over planned tool calls."""
    runtime_config = config or RuntimeConfig(max_turns=task.max_steps)
    steps = plan_task(task)
    messages = [AgentMessage(role="user", content=task.description)]

    if runtime_config.plan_only:
        prompt = build_agent_summary_prompt(task, steps, [])
        return AgentResult(
            task=task,
            steps=steps,
            tool_results=[],
            prompt=prompt,
            answer=None,
            messages=messages,
            turns=[],
            stop_reason=AgentStopReason.PLAN_ONLY,
        )

    tool_context = context or ToolContext(task.project_path, allow_tests=task.allow_tests)
    tool_results: list[ToolResult] = []
    turns: list[AgentTurn] = []
    stop_reason = AgentStopReason.FINAL

    for turn_index, step in enumerate(steps, start=1):
        if turn_index > runtime_config.max_turns:
            stop_reason = AgentStopReason.MAX_TURNS
            break

        tool_call = ToolCall.from_step(step)
        tool_call_id = f"turn-{turn_index}"
        messages.append(
            AgentMessage(
                role="assistant",
                content=tool_call.reason or f"Call tool {tool_call.name}.",
                tool_call_id=tool_call_id,
                tool_name=tool_call.name,
            )
        )
        result = execute_tool_call(
            task,
            tool_call,
            tools=tools,
            context=tool_context,
        )
        tool_results.append(result)
        turns.append(AgentTurn(index=turn_index, tool_call=tool_call, tool_result=result))
        messages.append(
            AgentMessage(
                role="tool",
                content=result.summary if result.ok else f"{result.summary} {result.error}",
                tool_call_id=tool_call_id,
                tool_name=tool_call.name,
            )
        )

    prompt = build_agent_summary_prompt(task, steps, tool_results)
    answer = llm_client.generate(prompt) if llm_client is not None else None
    if answer:
        messages.append(AgentMessage(role="assistant", content=answer))

    return AgentResult(
        task=task,
        steps=steps,
        tool_results=tool_results,
        prompt=prompt,
        answer=answer,
        messages=messages,
        turns=turns,
        stop_reason=stop_reason,
    )
