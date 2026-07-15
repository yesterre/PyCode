# PyCode 演示指南

这份指南用于本地自测、README 截图或面试演示。推荐先使用示例项目 `examples/demo_project`，因为它体积小，已经包含控制器、服务、模型、工具函数和测试文件，能展示 PyCode 从扫描到 Agent 计划的完整流程。

## 离线演示路径

离线演示不需要配置 LLM API，适合稳定展示核心能力。

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

`requirements.txt` 会以可编辑模式安装当前项目。启动 Web Demo 前也需要先执行这一步，避免 Streamlit 入口找不到 `pycode` 包。

```powershell
.\.venv\Scripts\python.exe -m pycode.cli index .\examples\demo_project
.\.venv\Scripts\python.exe -m pycode.cli graph .\examples\demo_project
.\.venv\Scripts\python.exe -m pycode.cli query entry .\examples\demo_project
.\.venv\Scripts\python.exe -m pycode.cli agent .\examples\demo_project "这个项目的入口在哪里？阅读顺序应该是怎样的？" --plan-only --show-context --rule-plan
```

演示时可以按这个顺序讲：`index` 证明项目能扫描并理解 Python 文件结构；`graph` 证明项目能把结构升级为关系图谱；`query entry` 展示图谱查询；`agent --plan-only --show-context --rule-plan` 展示 Agent 在不调用 LLM 的情况下如何规划分析步骤，并暴露 todo、trace 和 context 摘要。

如果终端 Rich 输出不方便截图，可以加 `--plain` 切换到普通文本：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli graph .\examples\demo_project --plain
```

## Web Demo

生成 index 和 graph 后，可以启动 Streamlit 页面：

```powershell
.\.venv\Scripts\streamlit.exe run .\ui\streamlit_app.py
```

页面建议按以下顺序展示：Project Overview 看整体统计和 `.pclens` 产物状态；File Tree 看文件结构和每个文件的 import/class/function 数量；Code Graph 看节点和边；Agent Run 输入示例问题，默认使用更安全的 plan-only 展示；Memory / Tasks 展示阶段五沉淀的项目记忆和任务依赖状态。

## 需要 LLM 的演示

如果已经配置 `OPENAI_API_KEY`，可以展示自然语言问答和真实 Agent 总结。

```powershell
.\.venv\Scripts\python.exe -m pycode.cli ask .\examples\demo_project "这个项目的入口在哪里？"
.\.venv\Scripts\python.exe -m pycode.cli explain .\examples\demo_project main.py
.\.venv\Scripts\python.exe -m pycode.cli onboard .\examples\demo_project
.\.venv\Scripts\python.exe -m pycode.cli impact .\examples\demo_project services/user_service.py
```

真实 Agent 总结示例：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli agent .\examples\demo_project "分析当前 git diff 是否影响用户服务逻辑"
```

如果要展示受控测试运行，需要显式传入 `--run-tests`：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli agent .\examples\demo_project "分析当前改动并运行相关测试" --run-tests
```

## 推荐示例问题

- 这个项目的入口在哪里？
- 给我一个新手阅读顺序。
- 修改 `services/user_service.py` 会影响哪里？
- 这次 git diff 有什么风险？
- 哪些工具被 Agent 调用了，分别产生了什么证据？

## 讲解重点

演示时不要把 PyCode 讲成“自动写代码工具”。更准确的定位是：它先把 Python 项目整理成可查询的结构化数据，再基于这些数据做问答、影响分析和 Agent 工具编排。当前最值得强调的是上下文选择、证据追踪和工程边界：LLM 只解释 PyCode 提供的有限上下文，Agent 默认不修改代码、不提交 git，也不默认运行测试。
