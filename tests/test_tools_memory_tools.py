from pathlib import Path

from pycode.tools import ToolContext
from pycode.tools.memory_tools import memory


def test_memory_tool_adds_lists_searches_and_loads_memories(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    context = ToolContext(project_path)

    created = memory(
        context,
        operation="add",
        name="Project Entry",
        memory_type="project",
        description="入口在 main.py",
        body="main.py is the project entry.",
        tags=["entry"],
    )
    listed = memory(context, operation="list")
    searched = memory(context, operation="search", query="入口")
    loaded = memory(context, operation="load", name="project-entry")

    assert created.ok is True
    assert created.data["memory"]["name"] == "project-entry"
    assert created.data["storage_dir"] == ".pclens/memory"
    assert listed.data["memories"][0]["type"] == "project"
    assert searched.data["memories"][0]["name"] == "project-entry"
    assert loaded.data["memory"]["body"] == "main.py is the project entry."


def test_memory_tool_rebuilds_index(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    context = ToolContext(project_path)
    memory(
        context,
        operation="add",
        name="No Auto Tests",
        memory_type="feedback",
        description="Do not run tests automatically.",
        body="Give test commands to the user.",
    )

    rebuilt = memory(context, operation="rebuild")

    assert rebuilt.ok is True
    assert rebuilt.summary == "Memory index rebuilt with 1 entries."
    assert rebuilt.data["evidence"] == [".pclens/memory/MEMORY.md"]


def test_memory_tool_requires_arguments(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    context = ToolContext(project_path)

    missing_name = memory(context, operation="add", memory_type="project", body="body")
    missing_type = memory(context, operation="add", name="name", body="body")
    missing_load_name = memory(context, operation="load")
    unsupported = memory(context, operation="delete", name="name")

    assert missing_name.ok is False
    assert missing_name.summary == "Memory name is required."
    assert missing_name.error == "operation='add' requires name."
    assert missing_type.ok is False
    assert missing_type.summary == "Memory memory_type is required."
    assert missing_type.error == "operation='add' requires memory_type."
    assert missing_load_name.ok is False
    assert missing_load_name.summary == "Memory name is required."
    assert missing_load_name.error == "operation='load' requires name."
    assert unsupported.ok is False
    assert unsupported.summary == "Unsupported memory operation."


def test_memory_tool_writes_only_project_internal_memory_files(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    context = ToolContext(project_path)

    result = memory(
        context,
        operation="add",
        name="Project Entry",
        memory_type="project",
        description="entry",
        body="entry",
    )

    assert result.ok is True
    assert (project_path / ".pclens" / "memory" / "project-entry.md").exists()
    assert not (tmp_path / "project-entry.md").exists()
