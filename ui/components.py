from __future__ import annotations

from typing import Any

from pycode.agent import AgentResult

from ui.data_loader import (
    ProjectUIData,
    build_file_tree_rows,
    build_graph_edge_rows,
    build_graph_node_rows,
    build_memory_rows,
    build_project_overview,
    build_task_rows,
    graph_edge_type_counts,
    graph_node_type_counts,
)


def render_project_overview(data: ProjectUIData) -> None:
    st = _streamlit()
    overview = build_project_overview(data)
    cols = st.columns(4)
    cols[0].metric("Python 文件", overview["python_files"])
    cols[1].metric("图谱节点", overview["graph_nodes"])
    cols[2].metric("图谱关系", overview["graph_edges"])
    cols[3].metric("项目记忆", overview["memories"])

    st.caption(f"项目路径：{overview['project_path']}")
    st.caption(f"index 文件：{overview['index_path']}")
    st.caption(f"graph 文件：{overview['graph_path']}")
    for error in overview["errors"]:
        st.warning(_localize_message(error))


def render_file_tree(data: ProjectUIData) -> None:
    st = _streamlit()
    rows = build_file_tree_rows(data.index)
    if not rows:
        st.info("还没有加载 index 数据。请先生成或检查 .pclens/index.json。")
        return
    st.dataframe(
        _rename_rows(
            rows,
            {
                "path": "路径",
                "imports": "import 数",
                "classes": "class 数",
                "functions": "function 数",
                "has_main_guard": "是否有 main guard",
            },
        ),
        use_container_width=True,
        hide_index=True,
    )


def render_code_graph(data: ProjectUIData) -> None:
    st = _streamlit()
    if data.graph is None:
        st.info("还没有加载代码图谱。请先生成或检查 .pclens/code_graph.json。")
        return

    col_a, col_b = st.columns(2)
    col_a.subheader("节点类型")
    col_a.dataframe(
        _rename_rows(_count_rows(graph_node_type_counts(data.graph)), _COUNT_LABELS),
        use_container_width=True,
        hide_index=True,
    )
    col_b.subheader("关系类型")
    col_b.dataframe(
        _rename_rows(_count_rows(graph_edge_type_counts(data.graph)), _COUNT_LABELS),
        use_container_width=True,
        hide_index=True,
    )

    st.subheader("节点列表")
    st.dataframe(
        _rename_rows(
            build_graph_node_rows(data.graph),
            {"id": "节点 ID", "type": "类型", "name": "名称", "path": "路径"},
        ),
        use_container_width=True,
        hide_index=True,
    )
    st.subheader("关系列表")
    st.dataframe(
        _rename_rows(
            build_graph_edge_rows(data.graph),
            {"source": "来源", "type": "关系类型", "target": "目标"},
        ),
        use_container_width=True,
        hide_index=True,
    )


def render_memory_and_tasks(data: ProjectUIData) -> None:
    st = _streamlit()
    st.subheader("项目记忆")
    memory_rows = build_memory_rows(data.memories)
    if memory_rows:
        st.dataframe(
            _rename_rows(
                memory_rows,
                {
                    "name": "名称",
                    "type": "类型",
                    "description": "说明",
                    "path": "路径",
                    "tags": "标签",
                },
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("还没有项目记忆。")

    st.subheader("Task DAG")
    task_rows = build_task_rows(data.tasks)
    if task_rows:
        st.dataframe(
            _rename_rows(
                _localized_status_rows(task_rows),
                {
                    "id": "任务 ID",
                    "title": "标题",
                    "status": "状态",
                    "owner": "负责人",
                    "blocked_by": "阻塞依赖",
                    "updated_at": "更新时间",
                },
            ),
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("还没有 Task DAG 文件。")


def render_agent_result(result: AgentResult, *, show_context: bool) -> None:
    st = _streamlit()
    st.subheader("Agent 摘要")
    cols = st.columns(5)
    cols[0].metric("步骤数", len(result.steps))
    cols[1].metric("运行轮次", len(result.turns))
    cols[2].metric("工具结果", len(result.tool_results))
    cols[3].metric("停止原因", str(result.stop_reason))
    cols[4].metric("Planner", _display_planner_source(result.planner_source))
    if result.planner_error:
        st.warning(f"LLM Planner 失败，已使用规则兜底：{result.planner_error}")

    st.markdown("### 执行步骤")
    st.dataframe(
        _rename_rows(
            _step_rows(result),
            {
                "index": "序号",
                "tool": "工具",
                "status": "状态",
                "summary": "摘要 / 原因",
            },
        ),
        use_container_width=True,
        hide_index=True,
    )

    if result.todos:
        st.markdown("### Todo 清单")
        st.dataframe(
            _rename_rows(
                _todo_rows(result),
                {
                    "id": "Todo ID",
                    "status": "状态",
                    "tool": "工具",
                    "title": "标题",
                    "error": "错误",
                },
            ),
            use_container_width=True,
            hide_index=True,
        )

    if result.trace is not None:
        st.markdown("### Trace 轨迹")
        st.dataframe(
            _rename_rows(
                _trace_rows(result),
                {
                    "turn": "轮次",
                    "tool": "工具",
                    "status": "状态",
                    "duration_ms": "耗时 ms",
                    "summary": "摘要",
                    "error": "错误",
                },
            ),
            use_container_width=True,
            hide_index=True,
        )

    if result.memory is not None:
        st.markdown("### Memory 记忆")
        st.json(result.memory.summary())

    if show_context and result.context is not None:
        st.markdown("### Context Sections 上下文片段")
        st.dataframe(
            _rename_rows(
                _context_rows(result),
                {
                    "name": "名称",
                    "placement": "位置",
                    "source": "来源",
                    "chars": "字符数",
                },
            ),
            use_container_width=True,
            hide_index=True,
        )
        if result.context.warnings:
            for warning in result.context.warnings:
                st.warning(warning)

    st.markdown("### 回答")
    st.write(result.answer or "当前是 plan-only 模式，没有生成 LLM 总结。")


def _step_rows(result: AgentResult) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, step in enumerate(result.steps, start=1):
        tool_result = (
            result.tool_results[index - 1]
            if index <= len(result.tool_results)
            else None
        )
        rows.append(
            {
                "index": index,
                "tool": step.tool,
                "status": _display_status(
                    "ok"
                    if tool_result and tool_result.ok
                    else "failed"
                    if tool_result
                    else "planned"
                ),
                "summary": tool_result.summary if tool_result else step.reason,
            }
        )
    return rows


def _todo_rows(result: AgentResult) -> list[dict[str, str]]:
    return [
        {
            "id": item.id,
            "status": _display_status(item.status),
            "tool": item.tool,
            "title": item.title or item.reason or "",
            "error": item.error or "",
        }
        for item in result.todos
    ]


def _trace_rows(result: AgentResult) -> list[dict[str, Any]]:
    if result.trace is None:
        return []
    return [
        {
            "turn": trace.turn_index,
            "tool": trace.tool,
            "status": _display_status(trace.status),
            "duration_ms": trace.duration_ms,
            "summary": trace.summary,
            "error": trace.error or "",
        }
        for trace in result.trace.tools
    ]


def _context_rows(result: AgentResult) -> list[dict[str, Any]]:
    if result.context is None:
        return []
    return [
        {
            "name": section.name,
            "placement": section.placement,
            "source": section.source,
            "chars": len(section.content),
        }
        for section in result.context.sections
    ]


def _count_rows(counts: dict[str, int]) -> list[dict[str, Any]]:
    return [
        {"type": key, "count": value}
        for key, value in sorted(counts.items())
    ]


def _rename_rows(rows: list[dict[str, Any]], labels: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {labels.get(key, key): value for key, value in row.items()}
        for row in rows
    ]


def _display_status(status: str) -> str:
    return {
        "ok": "成功",
        "failed": "失败",
        "planned": "已计划",
        "pending": "待执行",
        "in_progress": "执行中",
        "completed": "已完成",
        "denied": "已拒绝",
    }.get(status, status)


def _display_planner_source(source: str) -> str:
    return {
        "llm": "LLM",
        "rule": "规则",
        "fallback": "规则兜底",
    }.get(source, source)


def _localize_message(message: str) -> str:
    replacements = {
        "Project path does not exist": "项目路径不存在",
        "Project path is not a directory": "项目路径不是目录",
        "Missing index": "缺少 index 文件",
        "Missing graph": "缺少 graph 文件",
        "Failed to load index": "加载 index 失败",
        "Failed to load graph": "加载 graph 失败",
        "Failed to load memories": "加载项目记忆失败",
        "Failed to load tasks": "加载 Task DAG 失败",
    }
    text = message
    for english, chinese in replacements.items():
        text = text.replace(english, chinese)
    return text


def _localized_status_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    localized: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        if "status" in item:
            item["status"] = _display_status(str(item["status"]))
        localized.append(item)
    return localized


def _streamlit() -> Any:
    try:
        import streamlit as st
    except ImportError as exc:  # pragma: no cover - only hit without dependency.
        raise RuntimeError(
            "Streamlit 未安装。请运行 `python -m pip install -r requirements.txt`。"
        ) from exc
    return st


_COUNT_LABELS = {"type": "类型", "count": "数量"}
