from __future__ import annotations

from pathlib import Path

import streamlit as st

from pycode.agent import run_agent_task
from pycode.cli import graph_project, index_project
from pycode.llm_client import OpenAIResponsesClient

from ui.components import (
    render_agent_result,
    render_code_graph,
    render_file_tree,
    render_memory_and_tasks,
    render_project_overview,
)
from ui.data_loader import load_project_ui_data


def main() -> None:
    st.set_page_config(
        page_title="PyCode Agent 演示",
        layout="wide",
    )
    st.title("PyCode Agent 演示")
    st.caption("Python 代码库理解与改动影响分析 Agent 的阶段六可视化展示。")

    project_path = _sidebar_project_path()
    project = Path(project_path)

    with st.sidebar:
        st.divider()
        st.subheader("分析产物")
        col_a, col_b = st.columns(2)
        if col_a.button("生成 index", use_container_width=True):
            try:
                index_project(project)
                st.success("index.json 已生成。")
            except Exception as exc:
                st.error(f"生成 index 失败：{type(exc).__name__}: {exc}")
        if col_b.button("生成 graph", use_container_width=True):
            try:
                graph_project(project)
                st.success("code_graph.json 已生成。")
            except Exception as exc:
                st.error(f"生成 graph 失败：{type(exc).__name__}: {exc}")

    data = load_project_ui_data(project)
    tabs = st.tabs(
        [
            "项目概览",
            "文件树",
            "代码图谱",
            "Agent 运行",
            "记忆 / 任务",
        ]
    )

    with tabs[0]:
        render_project_overview(data)
    with tabs[1]:
        render_file_tree(data)
    with tabs[2]:
        render_code_graph(data)
    with tabs[3]:
        _render_agent_tab(project)
    with tabs[4]:
        render_memory_and_tasks(data)


def _sidebar_project_path() -> str:
    with st.sidebar:
        st.header("项目")
        return st.text_input(
            "项目路径",
            value=str(Path("examples/demo_project")),
            help="Python 项目的路径。演示页面会读取 .pclens/index.json 和 .pclens/code_graph.json。",
        )


def _render_agent_tab(project_path: Path) -> None:
    st.subheader("运行 Agent")
    task = st.text_area(
        "任务",
        value="这个项目的入口在哪里？阅读顺序应该是怎样的？",
        height=100,
    )
    col_a, col_b, col_c = st.columns(3)
    plan_only = col_a.checkbox("仅生成计划（plan-only）", value=True)
    run_tests = col_b.checkbox("允许运行测试（run-tests）", value=False)
    show_context = col_c.checkbox("显示上下文（show-context）", value=True)
    rule_plan = st.checkbox("强制使用规则计划（rule-plan）", value=False)
    model = st.text_input("模型", value="", help="可选的 OpenAI 模型名称。")

    if not st.button("运行 Agent", type="primary"):
        st.info("plan-only 只生成计划，不执行工具。未配置 OpenAI 凭据时会自动回退到规则计划，也可以勾选 rule-plan 离线演示。")
        return

    if not task.strip():
        st.warning("请输入 Agent 任务。")
        return

    try:
        client = None if plan_only and rule_plan else OpenAIResponsesClient(model=model or None)
        result = run_agent_task(
            task,
            project_path,
            allow_tests=run_tests,
            llm_client=client,
            plan_only=plan_only,
            use_llm_planner=not rule_plan,
        )
    except Exception as exc:
        st.error(f"Agent 运行失败：{type(exc).__name__}: {exc}")
        return

    render_agent_result(result, show_context=show_context)


if __name__ == "__main__":
    main()
