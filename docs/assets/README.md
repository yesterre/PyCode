# PyCode 展示素材目录

本目录用于存放阶段六之后的截图或 GIF。截图应来自当前真实功能，不使用过期输出或手工伪造界面。

建议补充的素材：

- `rich-index.png`：Rich CLI 的 `index` 输出。
- `rich-agent-trace.png`：Rich CLI 的 `agent --plan-only --show-context` 输出。
- `streamlit-overview.png`：Streamlit 的 Project Overview 页面。
- `streamlit-agent-run.png`：Streamlit 的 Agent Run 页面。

推荐截图前先运行：

```powershell
.\.venv\Scripts\python.exe -m pycode.cli index .\examples\demo_project
.\.venv\Scripts\python.exe -m pycode.cli graph .\examples\demo_project
.\.venv\Scripts\streamlit.exe run .\ui\streamlit_app.py
```
