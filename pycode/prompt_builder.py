from pycode.retriever import RetrievalResult
from pycode.evidence_pack import render_evidence, encoded


ANSWER_FORMAT = """回答要求：
1. 只能基于下方证据回答，不要假设未提供的仓库内容。
2. 如果证据不足，请明确说“不确定”，并说明缺少哪些索引、图谱或代码片段。
3. 回答必须包含“依据位置”，列出文件路径、函数名、类名或图谱节点。
4. 不要提出或执行任何代码修改。"""


def build_code_qa_prompt(retrieval: RetrievalResult) -> str:
    """Build a stable prompt from selected code context."""
    evidence = render_evidence(retrieval.items)
    if retrieval.pack is not None:
        evidence += "\n\nEvidence Pack: " + encoded(retrieval.pack.summary())
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
