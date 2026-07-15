# 阶段四增强开发计划：向 Agent Runtime Loop 转换

## Summary

本次增强可以开始做。目标不是推翻当前阶段四，而是把现有 `Plan-and-Solve Agent` 升级成“显式 Agent loop 架构”：保留规则 planner / executor / tools 的稳定基础，同时让 `agent` 命令真正继承阶段三的项目问答能力，并为后续 SDK、Memory、MCP、LLM function calling 留出清晰接口。

增强后，用户命令：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli agent .\examples\demo_project "这个项目的入口在哪？阅读顺序应该是怎样的？"
```

应能输出有效回答，并在 Evidence 中展示入口、阅读顺序和相关上下文来源。

## Key Changes

1. **补齐阶段三能力进入 Agent**
   - 扩展 `retrieve_context`，支持 `entry`、`onboard`、`explain`、`dependency`、`impact`、`general` 等 intent。
   - 对接阶段三已有能力：`retrieve_for_question`、`retrieve_explain`、`retrieve_onboard`、`retrieve_impact`。
   - 让 `agent` 能回答入口、阅读顺序、文件解释、依赖关系、影响分析、一般项目问题，而不是只处理当前几个固定任务。

2. **扩展 planner 的任务识别**
   - 新增或细化任务类型：`entry-question`、`onboard-question`、`explain-question`、`dependency-question`、`impact-question`、`diff-impact`、`test-coverage`、`test-failure`、`general-question`。
   - 对“入口在哪 + 阅读顺序”这类复合问题，规划多个上下文工具调用，例如先 `retrieve_context(intent="entry")`，再 `retrieve_context(intent="onboard")`。
   - 保留当前 diff、test、impact 类任务能力，避免阶段四已有功能倒退。

3. **引入轻量 Agent Runtime Loop**
   - 新增 `pycode/agent/runtime.py`，实现最小 loop：
     用户任务 -> 消息历史 -> 决策下一步工具 -> 执行工具 -> 追加 observation -> 判断停止 -> 生成最终回答。
   - 当前先采用“规则 planner 驱动的 hybrid loop”，不直接接 SDK function calling。
   - 未来可以把 planner 替换成 `LLMPlanner` / SDK tool-calling planner，而不用重写工具层和执行层。

4. **补充 Agent 类型和执行边界**
   - 在 `types.py` 中增加面向 loop 的结构：`AgentMessage`、`ToolCall`、`AgentTurn`、`AgentStopReason`、`RuntimeConfig`。
   - 在 executor 中抽出单步工具执行能力，例如 `execute_tool_call(...)`，供 runtime 每轮调用。
   - 新增或整理 `policy.py`：集中管理只读工具、测试工具、未来写操作权限。当前阶段默认不加入写文件/改代码工具。

5. **保持 CLI 兼容并增强输出**
   - `agent` 命令默认走新的 runtime loop。
   - `--plan-only` 显示预计工具调用，不执行工具、不调用 LLM。
   - `Evidence` 继续保留，并补充 runtime turn / tool observation 来源，方便用户理解 Agent 是如何得到答案的。
   - 阶段三旧命令如 `ask`、`explain`、`onboard`、`impact` 继续保留，作为专用入口。

6. **开发中同步阶段四文档**
   - 每完成一个增强模块，都更新 `docs/stage4_development_record.md` 的“开发中纪要”。
   - 文档中要记录：完成内容、涉及模块、命令变化、测试命令、已知限制。
   - 本次只更新“开发中”部分，不提前写“开发后总结”。

## Public Interfaces

- `pycode.agent.run_agent_task(...)` 保持可用，但内部改为调用 runtime，避免 CLI 和测试大面积改动。
- 新增 `run_agent_runtime(task, config, tools=None, llm_client=None)` 作为后续 SDK / MCP / Memory 接入点。
- `retrieve_context(...)` 增加 `intent` 取值，但保持现有调用方式兼容。
- `agent` CLI 参数保持：`--run-tests`、`--no-tests`、`--plan-only`、`--graph`、`--model`。

## Test Plan

- Planner 单测：覆盖入口、阅读顺序、解释、依赖、影响、diff、测试覆盖、测试失败、general fallback。
- `retrieve_context` 单测：验证 `entry`、`onboard`、`explain`、`impact`、`general` 能正确复用阶段三检索。
- Runtime 单测：验证一轮一工具、消息追加、工具失败、权限拒绝、max turns 停止、正常 final answer。
- CLI 单测：用 fake tools / mock LLM 验证 `agent "入口在哪？阅读顺序？"` 能进入 retrieve_context 链路，并输出 Evidence。
- 回归测试：阶段三 `ask/explain/onboard/impact` 入口继续可用。
- 验证命令优先使用 `python -B` smoke；pytest 若再次遇到 Windows `PermissionError: [WinError 5]`，在阶段四文档中如实记录。

## Assumptions

- 本次增强不直接接入外部 SDK、MCP 或持久记忆，只预留结构。
- 本次不加入写文件、自动修改代码、自动提交等高风险工具。
- 当前目标是“专业可演进的最小 Agent loop”，不是一次性实现完整 Claude Code 级别能力。
- `agent` 应成为统一项目理解入口，阶段三功能应被继承，而不是另起一套割裂流程。
