# PyCode 展示素材目录

本目录用于存放 README、演示指南或面试讲解中使用的截图和 GIF。素材是可选项，但如果补充，必须来自当前真实运行结果，不使用过期输出，不手工伪造界面。

建议素材：

- `rich-index.png`：Rich CLI 的 `index` 输出。
- `rich-agent-trace.png`：`agent --plan-only --show-context --rule-plan` 的 Rich 终端输出。
- `streamlit-overview.png`：Streamlit 的 Project Overview 页面。
- `streamlit-agent-run.png`：Streamlit 的 Agent Run 页面。

截图前建议先生成示例项目产物：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli index .\examples\demo_project
.\.venv\Scripts\python.exe -m pycode.cli graph .\examples\demo_project
.\.venv\Scripts\streamlit.exe run .\ui\streamlit_app.py
```

如果后续界面或 CLI 输出发生变化，应重新截图，避免文档展示和真实功能不一致。
