import json

from pycode.agent.types import AgentStep, AgentTask
from pycode.tools import ToolResult


AGENT_SUMMARY_RULES = """回答要求：
1. 只能基于工具结果总结，不要假设未提供的仓库内容。
2. 先给结论，再列风险点、依据位置和建议下一步。
3. 如果证据不足，请明确说明缺少哪些工具结果。
4. 不要声称已经修改代码或提交 git。
5. 如果测试没有被允许运行，只能说“未运行测试”。"""


def build_agent_summary_prompt(
    task: AgentTask,
    steps: list[AgentStep],
    tool_results: list[ToolResult],
) -> str:
    """Build a stable prompt for summarizing Agent tool evidence."""
    blocks = []
    for index, (step, result) in enumerate(zip(steps, tool_results), start=1):
        blocks.append(
            "\n".join(
                [
                    f"## 步骤 {index}: {step.tool}",
                    f"目的: {step.reason or 'N/A'}",
                    f"必须成功: {step.required}",
                    f"执行状态: {'成功' if result.ok else '失败'}",
                    f"摘要: {result.summary}",
                    f"错误: {result.error or 'N/A'}",
                    "数据:",
                    _format_data(result.data),
                ]
            )
        )

    evidence = "\n\n".join(blocks) or "没有执行任何工具。"
    return "\n\n".join(
        [
            "你是 PyCode 的开发任务分析 Agent。",
            AGENT_SUMMARY_RULES,
            f"用户任务: {task.description}",
            f"任务类型: {task.task_type}",
            f"项目路径: {task.project_path}",
            f"是否允许运行测试: {task.allow_tests}",
            f"指定图谱路径: {task.graph_path or '默认'}",
            "下面是 Agent 已执行工具得到的证据：",
            evidence,
        ]
    )


def _format_data(data: dict) -> str:
    if not data:
        return "N/A"
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


AGENT_SUMMARY_RULES = """Answer requirements:
1. Answer in the same language as the user task.
2. Base the answer only on tool results and clearly say when evidence is missing.
3. Give the conclusion first, then list evidence locations and suggested next steps.
4. Do not claim code was modified, committed, or tested unless the tool results show it.
5. If tests were not allowed or not run, say that tests were not run."""


def build_agent_summary_prompt(
    task: AgentTask,
    steps: list[AgentStep],
    tool_results: list[ToolResult],
) -> str:
    """Build a stable prompt for summarizing Agent runtime evidence."""
    blocks = []
    for index, (step, result) in enumerate(zip(steps, tool_results), start=1):
        blocks.append(
            "\n".join(
                [
                    f"## Step {index}: {step.tool}",
                    f"Purpose: {step.reason or 'N/A'}",
                    f"Required: {step.required}",
                    f"Status: {'ok' if result.ok else 'failed'}",
                    f"Summary: {result.summary}",
                    f"Error: {result.error or 'N/A'}",
                    "Data:",
                    _format_data(result.data),
                ]
            )
        )

    evidence = "\n\n".join(blocks) or "No tools were executed."
    return "\n\n".join(
        [
            "You are the PyCode project-understanding Agent.",
            AGENT_SUMMARY_RULES,
            f"User task: {task.description}",
            f"Task type: {task.task_type}",
            f"Project path: {task.project_path}",
            f"Tests allowed: {task.allow_tests}",
            f"Graph path: {task.graph_path or 'default'}",
            "The following evidence came from Agent tool calls:",
            evidence,
        ]
    )
