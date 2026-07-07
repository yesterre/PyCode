from pycode.agent.hooks import (
    HookContext,
    HookEventType,
    HookRegistry,
    create_default_hook_registry,
)
from pycode.agent.executor import execute_tool_call
from pycode.agent.memory import (
    MemoryRunInfo,
    MemoryStore,
    extract_memories,
    load_relevant_memories,
)
from pycode.agent.llm_planner import (
    PLANNER_SOURCE_FALLBACK,
    PLANNER_SOURCE_LLM,
    PLANNER_SOURCE_RULE,
    plan_task_with_llm,
)
from pycode.agent.planner import plan_task
from pycode.agent.prompts import build_agent_summary_context, render_agent_prompt
from pycode.agent.todo import TodoItem, TodoManager
from pycode.agent.trace import TraceRecorder
from pycode.agent.types import (
    AgentMessage,
    AgentResult,
    AgentStep,
    AgentStopReason,
    AgentTask,
    AgentTurn,
    RuntimeConfig,
    ToolCall,
)
from pycode.llm_client import LLMClient
from pycode.tools import TOOLS, ToolContext, ToolResult, ToolSpec
from pycode.tools.base import failure


def run_agent_runtime(
    task: AgentTask,
    config: RuntimeConfig | None = None,
    *,
    tools: dict[str, ToolSpec] | None = None,
    llm_client: LLMClient | None = None,
    context: ToolContext | None = None,
    hook_registry: HookRegistry | None = None,
) -> AgentResult:
    """Run a minimal Agent loop over planned tool calls.

    Default trace hooks are always retained; custom hook registries are appended.
    """
    runtime_config = config or RuntimeConfig(max_turns=task.max_steps)
    messages = [AgentMessage(role="user", content=task.description)]
    trace_recorder = TraceRecorder(task) if runtime_config.enable_trace else None
    hooks = create_default_hook_registry()
    tool_registry = tools or TOOLS
    if hook_registry is not None:
        hooks.extend(hook_registry)

    _trigger_hooks(
        hooks,
        HookContext(
            event_type=HookEventType.USER_PROMPT_SUBMIT,
            task=task,
            trace_recorder=trace_recorder,
        ),
    )
    memory_info, memory_index = _prepare_memory_context(
        task,
        messages,
        runtime_config,
        llm_client=llm_client if runtime_config.use_llm_planner else None,
        trace_recorder=trace_recorder,
        load_bodies=runtime_config.use_llm_planner and llm_client is not None,
    )
    steps, planner_source, planner_error = build_runtime_plan(
        task,
        runtime_config,
        tools=tool_registry,
        llm_client=llm_client,
        memory_index=memory_index,
        relevant_memories=memory_info.relevant_memories if memory_info else [],
        trace_recorder=trace_recorder,
    )
    todo_manager = TodoManager.from_steps(steps)
    _record_todo_event(
        trace_recorder,
        "TodoListCreated",
        "Agent todo list created from planned steps.",
        status="ok",
        data={"summary": todo_manager.summary(), "todos": todo_manager.to_dict()},
    )

    if runtime_config.plan_only:
        _finish_trace(
            hooks,
            task,
            trace_recorder,
            AgentStopReason.PLAN_ONLY,
        )
        agent_context = build_agent_summary_context(
            task,
            steps,
            [],
            memory_index=memory_index,
            relevant_memories=memory_info.relevant_memories if memory_info else [],
            trace=trace_recorder.trace if trace_recorder is not None else None,
            todos=list(todo_manager.items),
            tools=tool_registry,
        )
        prompt = render_agent_prompt(agent_context)
        return AgentResult(
            task=task,
            steps=steps,
            tool_results=[],
            prompt=prompt,
            answer=None,
            messages=messages,
            turns=[],
            stop_reason=AgentStopReason.PLAN_ONLY,
            trace=trace_recorder.trace if trace_recorder is not None else None,
            todos=list(todo_manager.items),
            memory=memory_info,
            context=agent_context,
            planner_source=planner_source,
            planner_error=planner_error,
        )

    tool_context = context or ToolContext(task.project_path, allow_tests=task.allow_tests)
    tool_context.state["todo_manager"] = todo_manager
    tool_results: list[ToolResult] = []
    turns: list[AgentTurn] = []
    stop_reason = AgentStopReason.FINAL

    for turn_index, step in enumerate(steps, start=1):
        if turn_index > runtime_config.max_turns:
            stop_reason = AgentStopReason.MAX_TURNS
            break

        tool_call = ToolCall.from_step(step)
        current_todo = _start_step_todo(todo_manager, step, trace_recorder)
        tool_call_id = f"turn-{turn_index}"
        messages.append(
            AgentMessage(
                role="assistant",
                content=tool_call.reason or f"Call tool {tool_call.name}.",
                tool_call_id=tool_call_id,
                tool_name=tool_call.name,
            )
        )
        pre_results = _trigger_hooks(
            hooks,
            HookContext(
                event_type=HookEventType.PRE_TOOL_USE,
                task=task,
                trace_recorder=trace_recorder,
                tool_call=tool_call,
                turn_index=turn_index,
            ),
        )
        denied = next((result for result in pre_results if not result.allowed), None)
        if denied is not None:
            denied_data = dict(denied.data)
            denied_data.pop("denied", None)
            denied_data.pop("denied_by", None)
            result = failure(
                tool_call.name,
                "Tool execution denied by hook.",
                denied.message or "A PreToolUse hook denied this tool call.",
                denied=True,
                denied_by=denied.denied_by or "hook",
                **denied_data,
            )
        else:
            result = execute_tool_call(
                task,
                tool_call,
                tools=tool_registry,
                context=tool_context,
            )
        _finish_step_todo(todo_manager, current_todo, result, trace_recorder)
        _trigger_hooks(
            hooks,
            HookContext(
                event_type=HookEventType.POST_TOOL_USE,
                task=task,
                trace_recorder=trace_recorder,
                tool_call=tool_call,
                tool_result=result,
                turn_index=turn_index,
            ),
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

    agent_context = build_agent_summary_context(
        task,
        steps,
        tool_results,
        memory_index=memory_index,
        relevant_memories=memory_info.relevant_memories if memory_info else [],
        trace=trace_recorder.trace if trace_recorder is not None else None,
        todos=list(todo_manager.items),
        tools=tool_registry,
    )
    prompt = render_agent_prompt(agent_context)
    answer = llm_client.generate(prompt) if llm_client is not None else None
    if answer:
        messages.append(AgentMessage(role="assistant", content=answer))
    _extract_memory_after_answer(
        task,
        messages,
        tool_results,
        answer,
        runtime_config,
        llm_client,
        memory_info,
        trace_recorder,
    )

    _finish_trace(hooks, task, trace_recorder, stop_reason)
    return AgentResult(
        task=task,
        steps=steps,
        tool_results=tool_results,
        prompt=prompt,
        answer=answer,
        messages=messages,
        turns=turns,
        stop_reason=stop_reason,
        trace=trace_recorder.trace if trace_recorder is not None else None,
        todos=list(todo_manager.items),
        memory=memory_info,
        context=agent_context,
        planner_source=planner_source,
        planner_error=planner_error,
    )


def build_runtime_plan(
    task: AgentTask,
    config: RuntimeConfig,
    *,
    tools: dict[str, ToolSpec],
    llm_client: LLMClient | None,
    memory_index: str = "",
    relevant_memories: list | None = None,
    trace_recorder: TraceRecorder | None = None,
) -> tuple[list[AgentStep], str, str | None]:
    if config.use_llm_planner and llm_client is not None:
        try:
            result = plan_task_with_llm(
                task,
                tools=tools,
                llm_client=llm_client,
                memory_index=memory_index,
                relevant_memories=relevant_memories or [],
            )
            _record_planner_event(
                trace_recorder,
                "LLMPlanGenerated",
                "LLM planner generated the Agent tool plan.",
                source=PLANNER_SOURCE_LLM,
                status="ok",
                steps=result.steps,
                warnings=result.warnings or [],
            )
            return result.steps, PLANNER_SOURCE_LLM, None
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            steps = plan_task(task)
            _record_planner_event(
                trace_recorder,
                "LLMPlanFallback",
                "LLM planner failed; rule planner fallback was used.",
                source=PLANNER_SOURCE_FALLBACK,
                status="fallback",
                steps=steps,
                error=error,
            )
            return steps, PLANNER_SOURCE_FALLBACK, error

    steps = plan_task(task)
    _record_planner_event(
        trace_recorder,
        "PlannerSelected",
        "Rule planner selected for this Agent run.",
        source=PLANNER_SOURCE_RULE,
        status="ok",
        steps=steps,
    )
    return steps, PLANNER_SOURCE_RULE, None


def _trigger_hooks(
    hooks: HookRegistry,
    context: HookContext,
):
    return hooks.trigger(context)


def _record_planner_event(
    trace_recorder: TraceRecorder | None,
    event_type: str,
    message: str,
    *,
    source: str,
    status: str,
    steps: list[AgentStep],
    error: str | None = None,
    warnings: list[str] | None = None,
) -> None:
    if trace_recorder is None:
        return
    trace_recorder.record_event(
        event_type,
        message,
        status=status,
        data={
            "planner_source": source,
            "steps": [
                {
                    "tool": step.tool,
                    "arguments": step.arguments,
                    "reason": step.reason,
                    "required": step.required,
                }
                for step in steps
            ],
            "error": error,
            "warnings": list(warnings or []),
        },
    )


def _finish_trace(
    hooks: HookRegistry,
    task: AgentTask,
    trace_recorder: TraceRecorder | None,
    stop_reason: str,
) -> None:
    _trigger_hooks(
        hooks,
        HookContext(
            event_type=HookEventType.STOP,
            task=task,
            trace_recorder=trace_recorder,
            stop_reason=stop_reason,
        ),
    )
    if trace_recorder is not None:
        status = "finished" if stop_reason != AgentStopReason.ERROR else "error"
        trace_recorder.finish(stop_reason, status=status)


def _start_step_todo(
    todo_manager: TodoManager,
    step: AgentStep,
    trace_recorder: TraceRecorder | None,
) -> TodoItem:
    if step.todo_id is None:
        raise ValueError(f"Planned step has no todo id: {step.tool}")
    item = todo_manager.start(step.todo_id)
    _record_todo_event(
        trace_recorder,
        "TodoStatusChanged",
        f"Todo {item.id} started.",
        status=item.status,
        data={"todo": item.to_dict(), "summary": todo_manager.summary()},
    )
    return item


def _finish_step_todo(
    todo_manager: TodoManager,
    item: TodoItem,
    result: ToolResult,
    trace_recorder: TraceRecorder | None,
) -> TodoItem:
    if result.ok:
        updated = todo_manager.complete(item.id)
        message = f"Todo {updated.id} completed."
    else:
        updated = todo_manager.fail(item.id, result.error or result.summary)
        message = f"Todo {updated.id} failed."
    _record_todo_event(
        trace_recorder,
        "TodoStatusChanged",
        message,
        status=updated.status,
        data={
            "todo": updated.to_dict(),
            "summary": todo_manager.summary(),
            "tool_result": {
                "tool": result.tool,
                "ok": result.ok,
                "summary": result.summary,
                "error": result.error,
                "denied": result.data.get("denied", False),
                "denied_by": result.data.get("denied_by"),
            },
        },
    )
    return updated


def _record_todo_event(
    trace_recorder: TraceRecorder | None,
    event_type: str,
    message: str,
    *,
    status: str,
    data: dict,
) -> None:
    if trace_recorder is None:
        return
    trace_recorder.record_event(
        event_type,
        message,
        status=status,
        data=data,
    )


def _prepare_memory_context(
    task: AgentTask,
    messages: list[AgentMessage],
    config: RuntimeConfig,
    *,
    llm_client: LLMClient | None,
    trace_recorder: TraceRecorder | None,
    load_bodies: bool,
) -> tuple[MemoryRunInfo | None, str]:
    if not config.enable_memory:
        return None, ""

    memory_info = MemoryRunInfo()
    memory_index = ""
    try:
        store = MemoryStore(task.project_path)
        memory_index = store.read_index_text()
        memory_info.index_entries = store.list_memories()
        _record_memory_event(
            trace_recorder,
            "MemoryIndexLoaded",
            "Project memory index loaded.",
            status="ok",
            data={"entries": len(memory_info.index_entries)},
        )
        if load_bodies and memory_info.index_entries:
            relevant, selection_error = load_relevant_memories(
                task.project_path,
                task_description=task.description,
                messages=messages,
                llm_client=llm_client,
                max_memories=config.max_relevant_memories,
            )
            memory_info.relevant_memories = relevant
            memory_info.selection_error = selection_error
            _record_memory_event(
                trace_recorder,
                "MemoryRelevantLoaded",
                "Relevant project memories loaded.",
                status="ok" if selection_error is None else "warning",
                data={
                    "loaded": len(relevant),
                    "selection_error": selection_error,
                    "memories": [item.name for item in relevant],
                },
            )
    except (PermissionError, ValueError, OSError) as exc:
        memory_info.selection_error = f"{type(exc).__name__}: {exc}"
        _record_memory_event(
            trace_recorder,
            "MemoryLoadFailed",
            "Project memory loading failed.",
            status="failed",
            data={"error": memory_info.selection_error},
        )
    return memory_info, memory_index


def _extract_memory_after_answer(
    task: AgentTask,
    messages: list[AgentMessage],
    tool_results: list[ToolResult],
    answer: str | None,
    config: RuntimeConfig,
    llm_client: LLMClient | None,
    memory_info: MemoryRunInfo | None,
    trace_recorder: TraceRecorder | None,
) -> None:
    if (
        memory_info is None
        or not config.enable_memory
        or not config.enable_memory_extraction
        or llm_client is None
        or answer is None
    ):
        return

    created, error = extract_memories(
        task.project_path,
        task_description=task.description,
        messages=messages,
        tool_results=tool_results,
        answer=answer,
        llm_client=llm_client,
    )
    memory_info.extracted_memories = created
    memory_info.extraction_error = error
    status = "ok" if error is None else "failed"
    _record_memory_event(
        trace_recorder,
        "MemoryExtracted",
        "Project memories extracted after Agent answer.",
        status=status,
        data={
            "created": len(created),
            "memories": [item.name for item in created],
            "error": error,
        },
    )


def _record_memory_event(
    trace_recorder: TraceRecorder | None,
    event_type: str,
    message: str,
    *,
    status: str,
    data: dict,
) -> None:
    if trace_recorder is None:
        return
    trace_recorder.record_event(event_type, message, status=status, data=data)
