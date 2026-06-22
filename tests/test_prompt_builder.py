from pycode.prompt_builder import build_code_qa_prompt
from pycode.retriever import ContextItem, RetrievalResult


def test_build_code_qa_prompt_contains_question_evidence_and_rules() -> None:
    retrieval = RetrievalResult(
        question="入口在哪里？",
        intent="entry",
        items=[
            ContextItem(
                title="File main.py",
                path="main.py",
                node_ids=["file:main.py", "func:main.py:main"],
                edges=["file:main.py --contains--> func:main.py:main"],
                snippet="1: def main():\n2:     pass",
                reason="入口候选。",
            )
        ],
    )

    prompt = build_code_qa_prompt(retrieval)

    assert "用户问题: 入口在哪里？" in prompt
    assert "依据位置" in prompt
    assert "file:main.py --contains--> func:main.py:main" in prompt
    assert "1: def main():" in prompt
    assert "不要提出或执行任何代码修改" in prompt
