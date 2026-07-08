# PyCode 技术概览

PyCode 的核心思路是先理解代码结构，再让问答或 Agent 基于结构化证据工作。普通 LLM 问答很容易把整个仓库当作一段长文本处理，成本高，也难追溯依据；PyCode 则把代码先整理成索引和图谱，再按问题选择上下文，这样回答更容易说明“依据来自哪里”。

## 数据流

整体流程可以概括为：

```text
Python 项目
-> 扫描 .py 文件
-> AST 解析 import / class / function / method
-> 保存 index.json
-> 构建 code_graph.json
-> 根据问题检索相关上下文
-> LLM 问答或 Agent 工具调用
-> 输出 answer / evidence / trace / todo / memory / context
```

`scanner.py` 负责找到项目里的 Python 文件，并忽略 `.git`、虚拟环境、缓存和 node_modules 等目录。`parser.py` 使用 Python 标准库 `ast` 解析每个文件，把 import、类、函数、方法和入口线索提取出来。`storage.py` 负责把索引和图谱保存到 `.pclens/`，让后续命令可以复用分析结果，而不是每次都从零开始。

在图谱层，`graph_builder.py` 会把文件、类、函数和方法转换为节点，把包含、导入、调用关系转换为边。`query.py` 在这个图谱上提供直接查询能力，例如某个文件导入了什么、被谁导入、某个函数调用了什么，以及哪些文件可能是入口。当前图谱使用 JSON 文件保存，选择这个方案是为了降低项目复杂度，便于学习和调试。

## 上下文与问答

阶段三之后，PyCode 不让 LLM 自己读取整个仓库，而是由 `retriever.py` 先根据问题选择相关文件、代码片段和图谱关系。`prompt_builder.py` 再把问题、上下文和证据位置组合成 prompt，交给 `llm_client.py` 调用模型。这样做虽然不如“把所有文件都交给模型”粗暴，但更符合代码理解工具的工程边界：上下文有限、来源清楚、结果可解释。

当前支持的问答入口包括 `ask`、`explain`、`onboard` 和 `impact`。它们复用同一套索引和图谱，只是在检索意图和 prompt 组织方式上有所区别。

## Agent 工作流

阶段四之后，PyCode 增加了一个轻量开发分析 Agent。它不是多 Agent 系统，也不会自动改代码，而是围绕一个任务生成工具计划，调用受控工具收集证据，再给出总结。常见工具包括读取文件、搜索代码、查询图谱、读取 git diff 和受控运行 pytest。

Agent 的运行流程大致是：

```text
用户任务
-> planner 生成工具步骤
-> policy 检查权限边界
-> executor 调用工具
-> runtime 汇总消息和工具结果
-> context builder 组装最终上下文
-> LLM 或 plan-only 输出结果
```

如果提供 LLM，Agent 会优先尝试 LLM Planner；如果没有配置 LLM，或者用户传入 `--rule-plan`，就使用规则 planner。`--plan-only` 用于只查看计划，不执行工具，也不生成真实 LLM 总结。

## 可观测能力

阶段五补齐了 Agent 的可观测信息。Trace 记录工具调用的开始、结束、耗时、状态和错误；Todo 把计划步骤映射成运行中的任务清单；Memory 用 Markdown 文件保存项目级知识；Task DAG 用 JSON 文件表达跨会话任务依赖；Context Section 把身份、工具、权限、项目状态、证据、trace、todo 和 memory 分层放进最终上下文。

这些能力不是完整的日志平台或项目管理系统，但已经能回答几个重要问题：Agent 准备做什么、实际调用了哪些工具、哪些工具失败了、回答依据来自哪些文件、哪些项目记忆参与了本次分析。

## 权限与异常边界

PyCode 当前通过 CLI 错误处理、工具返回值、权限策略和 trace 记录来控制边界。CLI 会把常见的文件缺失、权限问题、参数错误和运行时错误转成可读错误信息。工具层通过 `ToolResult` 返回成功状态、摘要、错误和结构化数据，避免把异常散落到上层流程。Agent 默认只做分析和建议，不自动修改代码、不自动提交 git，也不默认运行测试；运行测试必须显式使用 `--run-tests`。

这套边界还不等于完整沙箱，但对当前目标已经足够清楚：PyCode 是代码理解和开发分析助手，不是自动代码修改器，也不是 CI/CD 系统。

## 展示层

展示层分为两部分。Rich CLI 用表格、树和分区展示 index、graph、query 和 Agent 结果，同时保留 `--plain` 方便脚本化输出。Streamlit Demo 用页面展示项目概览、文件树、代码图谱、Agent 运行结果、Memory 和 Task DAG。Web UI 是演示型界面，重点是让别人快速看懂项目能力，而不是替代 IDE。
