from pathlib import Path

from pycode.agent.memory import (
    MemoryStore,
    MemoryType,
    build_memory_extraction_prompt,
    extract_memories,
    load_relevant_memories,
)
from pycode.tools.base import ToolResult


def test_memory_store_adds_lists_loads_and_rebuilds_index(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    store = MemoryStore(project_path)

    item = store.add_memory(
        name="Project Entry",
        memory_type=MemoryType.PROJECT,
        description="Project entry lives in main.py.",
        body="Use `main.py` as the first file to inspect.",
        tags=["entry"],
    )
    entries = store.list_memories()
    loaded = store.load_memory(item.name)

    assert item.name == "project-entry"
    assert entries[0].name == "project-entry"
    assert entries[0].type == MemoryType.PROJECT
    assert loaded.body == "Use `main.py` as the first file to inspect."
    assert (project_path / ".pclens" / "memory" / "project-entry.md").exists()
    index_text = (project_path / ".pclens" / "memory" / "MEMORY.md").read_text(
        encoding="utf-8"
    )
    assert "[project-entry](project-entry.md)" in index_text


def test_memory_store_validates_memory_type(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    store = MemoryStore(project_path)

    try:
        store.add_memory(
            name="bad",
            memory_type="workflow",
            description="bad",
            body="bad",
        )
    except ValueError as exc:
        assert "Unsupported memory type" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected unsupported memory type to fail.")


def test_memory_store_generates_unique_names(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    store = MemoryStore(project_path)

    first = store.add_memory(
        name="Test Command",
        memory_type=MemoryType.REFERENCE,
        description="pytest command",
        body="Run pytest.",
    )
    second = store.add_memory(
        name="Test Command",
        memory_type=MemoryType.REFERENCE,
        description="pytest command 2",
        body="Run targeted pytest.",
    )

    assert first.name == "test-command"
    assert second.name == "test-command-2"


def test_load_relevant_memories_uses_keyword_fallback_when_llm_fails(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    store = MemoryStore(project_path)
    store.add_memory(
        name="Project Entry",
        memory_type=MemoryType.PROJECT,
        description="入口 main.py",
        body="入口文件是 main.py。",
    )

    memories, error = load_relevant_memories(
        project_path,
        task_description="请分析项目入口",
        messages=[],
        llm_client=_FailingLLM(),
    )

    assert [memory.name for memory in memories] == ["project-entry"]
    assert "RuntimeError" in (error or "")


def test_extract_memories_writes_new_items_and_skips_duplicates(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    llm = _MemoryExtractionLLM()

    created, error = extract_memories(
        project_path,
        task_description="用户说以后不要自动运行测试",
        messages=[],
        tool_results=[ToolResult("retrieve_context", True, "ok")],
        answer="已记录偏好。",
        llm_client=llm,
    )
    created_again, error_again = extract_memories(
        project_path,
        task_description="用户说以后不要自动运行测试",
        messages=[],
        tool_results=[],
        answer="已记录偏好。",
        llm_client=llm,
    )

    assert error is None
    assert [item.type for item in created] == [MemoryType.FEEDBACK]
    assert created_again == []
    assert error_again is None


def test_memory_extraction_prompt_contains_four_types() -> None:
    prompt = build_memory_extraction_prompt(
        task_description="task",
        messages=[],
        tool_results=[],
        answer="answer",
        entries=[],
    )

    assert "user" in prompt
    assert "feedback" in prompt
    assert "project" in prompt
    assert "reference" in prompt


class _FailingLLM:
    def generate(self, prompt: str) -> str:
        raise RuntimeError("selection failed")


class _MemoryExtractionLLM:
    def generate(self, prompt: str) -> str:
        return """
        [
          {
            "name": "no-auto-tests",
            "type": "feedback",
            "description": "Do not run tests automatically.",
            "body": "The user prefers receiving test commands instead of automatic test execution.",
            "tags": ["tests"]
          }
        ]
        """
