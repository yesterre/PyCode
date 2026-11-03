from pycode.retriever import RetrievalResult


ANSWER_FORMAT = """回答要求：
1. 只能基于下方证据回答，不要假设未提供的仓库内容。
2. 如果证据不足，请明确说“不确定”，并说明缺少哪些索引、图谱或代码片段。
3. 回答必须包含“依据位置”，列出文件路径、函数名、类名或图谱节点。
4. 不要提出或执行任何代码修改。"""


def build_code_qa_prompt(retrieval: RetrievalResult) -> str:
    """Build a stable prompt from selected code context."""
    context_blocks = []
    for index, item in enumerate(retrieval.items, start=1):
        context_blocks.append(
            "\n".join(
                [
                    f"## 证据 {index}: {item.title}",
                    f"路径: {item.path or 'N/A'}",
                    f"选择原因: {item.reason or 'N/A'}",
                    f"Snapshot: {item.snapshot_id or 'unbound'}; Commit: {item.commit_sha or 'unbound'}",
                    f"Symbol: {item.symbol or 'unknown'}; Lines: {item.start_line}-{item.end_line}; Coverage: {item.coverage}",
                    f"Chunk IDs: {', '.join(item.chunk_ids) or 'unavailable'}",
                    f"Diagnostics: {', '.join(item.diagnostics) or 'none'}",
                    "节点:",
                    _format_list(item.node_ids),
                    "关系:",
                    _format_list(item.edges),
                    "代码片段:",
                    item.snippet or "N/A",
                ]
            )
        )

    evidence = "\n\n".join(context_blocks) or "没有检索到可用证据。"
    return "\n\n".join(
        [
            "你是 PyCode 的代码库理解助手。",
            ANSWER_FORMAT,
            f"问题类型: {retrieval.intent}",
            f"用户问题: {retrieval.question}",
            f"Retrieval status: {retrieval.status}; Diagnostics: {', '.join(retrieval.diagnostics) or 'none'}",
            "Graph relations are stored static-analysis hints; their runtime resolution is unverified. A name or keyword match is not proof of a call.",
            "下面是 PyCode 根据 index.json 和 code_graph.json 选择出的有限上下文：",
            evidence,
        ]
    )


def _format_list(items: list[str]) -> str:
    if not items:
        return "- N/A"
    return "\n".join(f"- {item}" for item in items)
