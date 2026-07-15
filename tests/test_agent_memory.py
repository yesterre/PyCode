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
        memory_id="Project Entry",
        memory_type=MemoryType.PROJECT,
        title="Project Entry",
        summary="Project entry lives in main.py.",
        body="Use `main.py` as the first file to inspect.",
        tags=["entry"],
        confidence=0.9,
        related_files=["main.py"],
    )
    entries = store.list_memories()
    loaded = store.load_memory(item.id)

    assert item.id == "project-entry"
    assert entries[0].id == "project-entry"
    assert entries[0].type == MemoryType.PROJECT
    assert entries[0].confidence == 0.9
    assert entries[0].related_files == ["main.py"]
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
            memory_id="bad",
            memory_type="legacy",
            title="bad",
            summary="bad",
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
        memory_id="Test Command",
        memory_type=MemoryType.WORKFLOW,
        title="Test Command",
        summary="pytest command",
        body="Run pytest.",
    )
    second = store.add_memory(
        memory_id="Test Command",
        memory_type=MemoryType.WORKFLOW,
        title="Test Command",
        summary="pytest command 2",
        body="Run targeted pytest.",
    )

    assert first.id == "test-command"
    assert second.id == "test-command-2"


def test_load_relevant_memories_uses_keyword_fallback_when_llm_fails(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    store = MemoryStore(project_path)
    store.add_memory(
        memory_id="Project Entry",
        memory_type=MemoryType.PROJECT,
        title="Project Entry",
        summary="入口 main.py",
        body="入口文件是 main.py。",
    )

    memories, error = load_relevant_memories(
        project_path,
        task_description="请分析项目入口",
        messages=[],
        llm_client=_FailingLLM(),
    )

    assert [memory.id for memory in memories] == ["project-entry"]
    assert "Memory selection LLM failed: RuntimeError" in (error or "")


def test_load_relevant_memories_reports_parse_failure_and_uses_fallback(
    tmp_path: Path,
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    store = MemoryStore(project_path)
    store.add_memory(
        memory_id="Project Entry",
        memory_type=MemoryType.PROJECT,
        title="Project Entry",
        summary="entry main.py",
        body="Entry file is main.py.",
    )

    memories, error = load_relevant_memories(
        project_path,
        task_description="entry",
        messages=[],
        llm_client=_BadJsonLLM(),
    )

    assert [memory.id for memory in memories] == ["project-entry"]
    assert "Memory selection parse failed: ValueError" in (error or "")


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
    assert [item.type for item in created] == [MemoryType.PREFERENCE]
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

    assert "project" in prompt
    assert "workflow" in prompt
    assert "analysis" in prompt
    assert "preference" in prompt
    assert "limitation" in prompt


class _FailingLLM:
    def generate(self, prompt: str) -> str:
        raise RuntimeError("selection failed")


class _BadJsonLLM:
    def generate(self, prompt: str) -> str:
        return "not json"


class _MemoryExtractionLLM:
    def generate(self, prompt: str) -> str:
        return """
        [
          {
            "id": "no-auto-tests",
            "type": "preference",
            "title": "No Auto Tests",
            "summary": "Do not run tests automatically.",
            "body": "The user prefers receiving test commands instead of automatic test execution.",
            "confidence": 1.0,
            "related_files": [],
            "tags": ["tests"]
          }
        ]
        """
