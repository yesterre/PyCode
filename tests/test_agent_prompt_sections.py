from pycode.agent.memory import MemoryItem, MemoryType
from pycode.agent.prompt_sections import (
    memory_index_section,
    relevant_memories_section,
    tool_results_section,
    trace_section,
)


def test_memory_index_section_uses_system_placement() -> None:
    section = memory_index_section("- [project-entry](project-entry.md) - Entry point")

    assert section is not None
    assert section.name == "memory_index"
    assert section.placement == "system"
    assert "Project memory index:" in section.content


def test_relevant_memories_section_uses_user_placement_and_tags() -> None:
    section = relevant_memories_section(
        [
            MemoryItem(
                name="project-entry",
                type=MemoryType.PROJECT,
                description="Entry point",
                body="main.py is the entry point.",
                path="project-entry.md",
            )
        ]
    )

    assert section is not None
    assert section.name == "relevant_memories"
    assert section.placement == "user"
    assert "<relevant_memories>" in section.content
    assert "main.py is the entry point." in section.content


def test_empty_dynamic_sections_are_not_rendered() -> None:
    assert memory_index_section("") is None
    assert relevant_memories_section([]) is None
    assert tool_results_section([], []) is None
    assert trace_section(None) is None
