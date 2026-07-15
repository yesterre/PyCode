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
    plan_next_action_with_llm,
    plan_task_with_llm,
)
from pycode.agent.planner import decide_next_action, plan_task
from pycode.agent.prompts import build_agent_summary_context, render_agent_prompt
from pycode.agent.todo import TodoItem, TodoManager
from pycode.agent.trace import TraceRecorder
from pycode.agent.types import (
    AgentAction,
    AgentActionType,
    AgentMessage,
    AgentObservation,
    AgentResult,
    AgentStep,
    AgentStopReason,
    AgentTask,
    AgentTurn,
    RuntimeConfig,
    ToolCall,
)
from pycode.llm_client import LLMClient, classify_llm_error
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
    """Run an observe-decide-act Agent loop over planned tool calls.

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
    loop_result = _run_observe_decide_act_loop(
        task,
        runtime_config,
        steps,
        todo_manager,
        memory_info,
        memory_index,
        messages,
        hooks,
        tool_registry,
        tool_context,
        trace_recorder,
        llm_client,
    )
    tool_results = loop_result["tool_results"]
    turns = loop_result["turns"]
    observations = loop_result["observations"]
    stop_reason = loop_result["stop_reason"]
    direct_answer = loop_result["answer"]
    loop_planner_source = loop_result["planner_source"]
    loop_planner_error = loop_result["planner_error"]

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
    if direct_answer is not None:
        answer = direct_answer
    elif llm_client is not None and stop_reason != AgentStopReason.ERROR:
        try:
            answer = llm_client.generate(prompt)
        except Exception as exc:
            summary_error = f"{type(exc).__name__}: {exc}"
            _record_loop_event(
                trace_recorder,
                "LLMSummaryFailed",
                "LLM summary generation failed after Agent loop.",
                status="failed",
                data={
                    "error": summary_error,
                    "error_category": classify_llm_error(exc),
                },
            )
            answer = None
            loop_planner_error = loop_planner_error or summary_error
    else:
        answer = None
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
        observations=observations,
        stop_reason=stop_reason,
        trace=trace_recorder.trace if trace_recorder is not None else None,
        todos=list(todo_manager.items),
        memory=memory_info,
        context=agent_context,
        planner_source=loop_planner_source or planner_source,
        planner_error=loop_planner_error or planner_error,
    )


def _run_observe_decide_act_loop(
    task: AgentTask,
    config: RuntimeConfig,
    steps: list[AgentStep],
    todo_manager: TodoManager,
    memory_info: MemoryRunInfo | None,
    memory_index: str,
    messages: list[AgentMessage],
    hooks: HookRegistry,
    tool_registry: dict[str, ToolSpec],
    tool_context: ToolContext,
    trace_recorder: TraceRecorder | None,
    llm_client: LLMClient | None,
) -> dict:
    tool_results: list[ToolResult] = []
    turns: list[AgentTurn] = []
    observations: list[AgentObservation] = []
    stop_reason: str = AgentStopReason.FINAL
    answer: str | None = None
    planner_source = PLANNER_SOURCE_RULE
    planner_error: str | None = None

    for turn_index in range(1, config.max_turns + 1):
        _record_loop_event(
            trace_recorder,
            "TurnStarted",
            f"Agent turn {turn_index} started.",
            status="running",
            data={"turn_index": turn_index},
        )
        turn_context = build_agent_summary_context(
            task,
            steps,
            tool_results,
            memory_index=memory_index,
            relevant_memories=memory_info.relevant_memories if memory_info else [],
            trace=trace_recorder.trace if trace_recorder is not None else None,
            todos=list(todo_manager.items),
            tools=tool_registry,
            turn_index=turn_index,
        )
        _record_context_event(trace_recorder, turn_index, turn_context)
        action, decision_info = _decide_next_action(
            task,
            config,
            turn_context,
            steps,
            turns,
            tool_results,
            todo_manager,
            tool_registry,
            llm_client,
            turn_index,
            trace_recorder,
        )
        planner_source = decision_info.get("planner_source") or planner_source
        planner_error = decision_info.get("planner_error") or planner_error
        _record_next_action_event(trace_recorder, turn_index, action, decision_info)

        if action.type == AgentActionType.FINAL_ANSWER:
            stop_reason = action.stop_reason or AgentStopReason.FINAL
            answer = action.answer
            turns.append(
                AgentTurn(
                    index=turn_index,
                    action=action,
                    status="final",
                )
            )
            _record_stop_decision(trace_recorder, turn_index, action, stop_reason)
            break
        if action.type == AgentActionType.STOP_WITH_ERROR:
            stop_reason = action.stop_reason or AgentStopReason.ERROR
            planner_error = planner_error or action.error or action.reason
            turns.append(
                AgentTurn(
                    index=turn_index,
                    action=action,
                    status="failed",
                )
            )
            _record_stop_decision(trace_recorder, turn_index, action, stop_reason)
            break
        if action.type == AgentActionType.NO_OP:
            stop_reason = action.stop_reason or AgentStopReason.NO_ACTION
            turns.append(
                AgentTurn(
                    index=turn_index,
                    action=action,
                    status="skipped",
                )
            )
            _record_stop_decision(trace_recorder, turn_index, action, stop_reason)
            break
        if action.type != AgentActionType.TOOL_CALL or action.tool_call is None:
            stop_reason = AgentStopReason.ERROR
            planner_error = planner_error or f"Unsupported action type: {action.type}"
            _record_stop_decision(
                trace_recorder,
                turn_index,
                AgentAction.stop_error(f"Unsupported action type: {action.type}"),
                stop_reason,
            )
            break

        result = _execute_tool_action(
            task,
            action,
            steps,
            turns,
            todo_manager,
            messages,
            hooks,
            tool_registry,
            tool_context,
            trace_recorder,
            turn_index,
        )
        observation = AgentObservation(
            turn_index=turn_index,
            action_type=action.type,
            tool_result=result,
            summary=result.summary,
            ok=result.ok,
            error=result.error,
        )
        observations.append(observation)
        tool_results.append(result)
        turns.append(
            AgentTurn(
                index=turn_index,
                tool_call=action.tool_call,
                tool_result=result,
                action=action,
                observation=observation,
                status="observed" if result.ok else "failed",
            )
        )
        _record_observation_event(trace_recorder, observation)
        _record_loop_event(
            trace_recorder,
            "TurnFinished",
            f"Agent turn {turn_index} finished.",
            status="ok" if result.ok else "failed",
            data={"turn_index": turn_index, "tool": result.tool},
        )
        if (
            decision_info.get("planner_source") == PLANNER_SOURCE_RULE
            and len(tool_results) >= len(steps)
        ):
            stop_reason = AgentStopReason.FINAL
            _record_stop_decision(
                trace_recorder,
                turn_index,
                AgentAction.final("All planned tool steps have been observed."),
                stop_reason,
            )
            break
    else:
        stop_reason = AgentStopReason.MAX_TURNS
        _record_loop_event(
            trace_recorder,
            "StopDecided",
            "Maximum Agent turns reached.",
            status="max_turns",
            data={"max_turns": config.max_turns},
        )

    return {
        "tool_results": tool_results,
        "turns": turns,
        "observations": observations,
        "stop_reason": stop_reason,
        "answer": answer,
        "planner_source": planner_source,
        "planner_error": planner_error,
    }


def _execute_tool_action(
    task: AgentTask,
    action: AgentAction,
    steps: list[AgentStep],
    turns: list[AgentTurn],
    todo_manager: TodoManager,
    messages: list[AgentMessage],
    hooks: HookRegistry,
    tool_registry: dict[str, ToolSpec],
    tool_context: ToolContext,
    trace_recorder: TraceRecorder | None,
    turn_index: int,
) -> ToolResult:
    tool_call = action.tool_call
    if tool_call is None:
        return failure(
            "agent_action",
            "Agent action is invalid.",
            "Tool action has no tool call.",
        )

    step = _step_for_action(action, steps, turns)
    current_todo = _start_step_todo(todo_manager, step, trace_recorder)
    tool_call_id = f"turn-{turn_index}"
    messages.append(
        AgentMessage(
            role="assistant",
            content=action.reason or tool_call.reason or f"Call tool {tool_call.name}.",
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
    _record_policy_event(trace_recorder, turn_index, tool_call, result)
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
    messages.append(
        AgentMessage(
            role="tool",
            content=result.summary if result.ok else f"{result.summary} {result.error}",
            tool_call_id=tool_call_id,
            tool_name=tool_call.name,
        )
    )
    return result


def _decide_next_action(
    task: AgentTask,
    config: RuntimeConfig,
    turn_context,
    steps: list[AgentStep],
    turns: list[AgentTurn],
    tool_results: list[ToolResult],
    todo_manager: TodoManager,
    tool_registry: dict[str, ToolSpec],
    llm_client: LLMClient | None,
    turn_index: int,
    trace_recorder: TraceRecorder | None,
) -> tuple[AgentAction, dict]:
    if config.use_llm_planner and llm_client is not None:
        _record_loop_event(
            trace_recorder,
            "LLMNextActionStarted",
            f"LLM next-action planning started for turn {turn_index}.",
            status="running",
            data={"turn_index": turn_index},
        )
        try:
            result = plan_next_action_with_llm(
                task,
                agent_context=turn_context,
                tools=tool_registry,
                llm_client=llm_client,
                turn_index=turn_index,
                max_turns=config.max_turns,
                steps=steps,
                turns=turns,
                tool_results=tool_results,
                todos=list(todo_manager.items),
            )
            _record_loop_event(
                trace_recorder,
                "LLMNextActionFinished",
                f"LLM next-action planning finished for turn {turn_index}.",
                status="ok",
                data={
                    "turn_index": turn_index,
                    "action_type": str(result.action.type),
                    "raw_response": result.raw_response,
                    "warnings": list(result.warnings or []),
                },
            )
            return result.action, {
                "planner_source": PLANNER_SOURCE_LLM,
                "fallback_used": False,
                "raw_response": result.raw_response,
                "schema_error": None,
                "planner_error": None,
            }
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            category = classify_llm_error(exc)
            _record_loop_event(
                trace_recorder,
                "LLMNextActionSchemaFailed",
                "LLM next-action planning failed; rule planner fallback will be used.",
                status="failed",
                data={
                    "turn_index": turn_index,
                    "error": error,
                    "error_category": category,
                },
            )
            fallback_action = decide_next_action(
                task,
                steps,
                turns,
                tool_results,
                turn_index,
            )
            _record_loop_event(
                trace_recorder,
                "LLMNextActionFallback",
                f"Rule next-action planner used for turn {turn_index}.",
                status="fallback",
                data={
                    "turn_index": turn_index,
                    "error": error,
                    "fallback_action_type": str(fallback_action.type),
                },
            )
            return fallback_action, {
                "planner_source": PLANNER_SOURCE_FALLBACK,
                "fallback_used": True,
                "raw_response": None,
                "schema_error": error,
                "planner_error": error,
            }

    action = decide_next_action(
        task,
        steps,
        turns,
        tool_results,
        turn_index,
    )
    return action, {
        "planner_source": PLANNER_SOURCE_RULE,
        "fallback_used": False,
        "raw_response": None,
        "schema_error": None,
        "planner_error": None,
    }


def _step_for_action(
    action: AgentAction,
    steps: list[AgentStep],
    turns: list[AgentTurn],
) -> AgentStep | None:
    tool_name = action.tool_call.name if action.tool_call is not None else None
    executed_tool_turns = [
        turn for turn in turns if turn.tool_call is not None and turn.tool_result is not None
    ]
    executed_count = len(executed_tool_turns)
    for step in steps[executed_count:]:
        if step.tool == tool_name:
            return step
    index = len(executed_tool_turns)
    if index >= len(steps):
        return None
    return steps[index]


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


def _record_loop_event(
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


def _record_next_action_event(
    trace_recorder: TraceRecorder | None,
    turn_index: int,
    action: AgentAction,
    decision_info: dict | None = None,
) -> None:
    decision_info = decision_info or {}
    data = {
        "turn_index": turn_index,
        "action_type": str(action.type),
        "reason": action.reason,
        "stop_reason": action.stop_reason,
        "error": action.error,
        "planner_source": decision_info.get("planner_source"),
        "fallback_used": decision_info.get("fallback_used", False),
        "schema_error": decision_info.get("schema_error"),
        "raw_response": decision_info.get("raw_response"),
    }
    if action.tool_call is not None:
        data["tool"] = action.tool_call.name
        data["arguments"] = action.tool_call.arguments
    _record_loop_event(
        trace_recorder,
        "NextActionDecided",
        f"Next action decided for turn {turn_index}.",
        status="ok",
        data=data,
    )


def _record_policy_event(
    trace_recorder: TraceRecorder | None,
    turn_index: int,
    tool_call: ToolCall,
    result: ToolResult,
) -> None:
    denied = bool(result.data.get("denied"))
    _record_loop_event(
        trace_recorder,
        "PolicyDecision",
        "Tool policy decision recorded.",
        status="denied" if denied else "allowed",
        data={
            "turn_index": turn_index,
            "tool": tool_call.name,
            "denied": denied,
            "denied_by": result.data.get("denied_by"),
            "summary": result.summary,
            "error": result.error,
        },
    )


def _record_observation_event(
    trace_recorder: TraceRecorder | None,
    observation: AgentObservation,
) -> None:
    tool = observation.tool_result.tool if observation.tool_result is not None else None
    _record_loop_event(
        trace_recorder,
        "ObservationRecorded",
        f"Observation recorded for turn {observation.turn_index}.",
        status="ok" if observation.ok else "failed",
        data={
            "turn_index": observation.turn_index,
            "action_type": observation.action_type,
            "tool": tool,
            "summary": observation.summary,
            "error": observation.error,
        },
    )


def _record_context_event(
    trace_recorder: TraceRecorder | None,
    turn_index: int,
    context,
) -> None:
    _record_loop_event(
        trace_recorder,
        "ContextAssembled",
        f"Context assembled for turn {turn_index}.",
        status="ok" if not context.warnings else "warning",
        data={
            "turn_index": turn_index,
            "sections": [
                {
                    "name": section.name,
                    "source": section.source,
                    "placement": section.placement,
                    "priority": section.priority,
                    "included": section.included,
                    "reason": section.reason,
                    "size_estimate": section.size_estimate,
                }
                for section in context.sections
            ],
            "skipped": [
                {
                    "name": section.name,
                    "reason": section.reason,
                }
                for section in context.skipped_sections
            ],
            "warnings": list(context.warnings),
        },
    )


def _record_stop_decision(
    trace_recorder: TraceRecorder | None,
    turn_index: int,
    action: AgentAction,
    stop_reason: str,
) -> None:
    _record_loop_event(
        trace_recorder,
        "StopDecided",
        f"Agent loop stop decided at turn {turn_index}.",
        status=str(stop_reason),
        data={
            "turn_index": turn_index,
            "action_type": str(action.type),
            "reason": action.reason,
            "stop_reason": stop_reason,
            "error": action.error,
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
    step: AgentStep | None,
    trace_recorder: TraceRecorder | None,
) -> TodoItem | None:
    if step is None:
        return None
    if step.todo_id is None:
        return None
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
    item: TodoItem | None,
    result: ToolResult,
    trace_recorder: TraceRecorder | None,
) -> TodoItem | None:
    if item is None:
        return None
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
                    "memories": [item.id for item in relevant],
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
            "memories": [item.id for item in created],
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
