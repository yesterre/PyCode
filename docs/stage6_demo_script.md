# 阶段六 Demo 演示脚本

这个脚本用于面试、README 截图或自测演示。目标是在几分钟内展示 PyCode 的核心价值：能扫描项目、构建代码图谱、用 Agent 规划分析任务，并通过终端和 Web 页面展示过程数据。

## 1. 安装依赖

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 2. 生成示例项目索引

```powershell
.\.venv\Scripts\python.exe -m pycode.cli index .\examples\demo_project
```

讲解点：

- PyCode 会递归扫描 Python 文件。
- 输出中能看到文件数量、import、class、function 统计。
- `.pclens/index.json` 是后续问答和图谱的基础。

## 3. 生成代码图谱

```powershell
.\.venv\Scripts\python.exe -m pycode.cli graph .\examples\demo_project
```

讲解点：

- 图谱由 nodes 和 edges 组成。
- 当前支持 file、class、function、method 节点。
- 当前支持 contains、imports、calls 等关系。

## 4. 查询入口候选文件

```powershell
.\.venv\Scripts\python.exe -m pycode.cli query entry .\examples\demo_project
```

讲解点：

- 入口识别基于文件名、main 函数和 `if __name__ == "__main__"` 等静态线索。
- 这是阶段二图谱查询能力的直接展示。

## 5. 运行 Agent 计划模式

```powershell
.\.venv\Scripts\python.exe -m pycode.cli agent .\examples\demo_project "这个项目的入口在哪里？阅读顺序应该是怎样的？" --plan-only --show-context
```

讲解点：

- `plan-only` 不调用 LLM，也不执行实际工具。
- 输出能展示 Agent 准备调用哪些工具。
- 阶段五的 todo、trace、context section 会进入展示层。

## 6. 启动 Web Demo

```powershell
.\.venv\Scripts\streamlit.exe run .\ui\streamlit_app.py
```

讲解顺序：

1. Project Overview：项目统计和 `.pclens` 产物状态。
2. File Tree：文件结构和每个文件的结构信息。
3. Code Graph：节点、边和关系类型统计。
4. Agent Run：输入示例问题，默认用 `plan-only` 安全展示。
5. Memory / Tasks：展示阶段五沉淀的 memory 和 Task DAG。

## 7. 推荐示例问题

- 这个项目的入口在哪里？
- 给我一个新手阅读顺序。
- 修改 `services/user_service.py` 会影响哪里？
- 这次 git diff 有什么风险？
- 哪些工具被 Agent 调用了，分别产生了什么证据？

## 8. 当前边界说明

- 当前主要支持 Python 项目。
- 静态 AST 分析不能覆盖所有动态调用。
- Agent 默认不自动修改代码、不自动提交 git。
- Web UI 是展示型 Demo，不是完整 IDE。
- 真实 LLM 总结需要配置 OpenAI 环境变量；`plan-only` 模式不需要。
