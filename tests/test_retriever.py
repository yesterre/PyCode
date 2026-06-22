from pathlib import Path

from pycode.models import ClassInfo, CodeGraph, FileInfo, GraphEdge, GraphNode, ProjectIndex
from pycode.retriever import (
    retrieve_explain,
    retrieve_for_question,
    retrieve_impact,
    retrieve_onboard,
)


def test_retrieve_for_entry_question_selects_entry_candidates(tmp_path: Path) -> None:
    project_path, index, graph = _sample_project(tmp_path)

    result = retrieve_for_question("这个项目的入口在哪里？", project_path, index, graph)

    assert result.intent == "entry"
    assert [item.path for item in result.items] == ["main.py"]
    assert "file:main.py" in result.evidence
    assert "1: from services.user_service import UserService" in result.items[0].snippet


def test_retrieve_explain_only_returns_target_file(tmp_path: Path) -> None:
    project_path, index, graph = _sample_project(tmp_path)

    result = retrieve_explain("services/user_service.py", project_path, index, graph)

    assert result.intent == "explain"
    assert [item.path for item in result.items] == ["services/user_service.py"]
    assert "main.py" not in [item.path for item in result.items]


def test_retrieve_impact_includes_imports_and_imported_by(tmp_path: Path) -> None:
    project_path, index, graph = _sample_project(tmp_path)

    result = retrieve_impact("services/user_service.py", project_path, index, graph)

    assert result.intent == "impact"
    assert [item.path for item in result.items] == [
        "services/user_service.py",
        "main.py",
        "models/user.py",
    ]
    assert (
        "file:main.py --imports--> file:services/user_service.py"
        in result.evidence
    )


def test_retrieve_onboard_starts_from_entry_and_neighbors(tmp_path: Path) -> None:
    project_path, index, graph = _sample_project(tmp_path)

    result = retrieve_onboard(project_path, index, graph)

    assert result.intent == "onboard"
    assert result.items[0].path == "main.py"
    assert "services/user_service.py" in [item.path for item in result.items]


def _sample_project(tmp_path: Path) -> tuple[Path, ProjectIndex, CodeGraph]:
    project_path = tmp_path / "demo_project"
    service_dir = project_path / "services"
    model_dir = project_path / "models"
    service_dir.mkdir(parents=True)
    model_dir.mkdir(parents=True)
    (project_path / "main.py").write_text(
        "\n".join(
            [
                "from services.user_service import UserService",
                "",
                "def main():",
                "    return UserService().get_user()",
            ]
        ),
        encoding="utf-8",
    )
    (service_dir / "user_service.py").write_text(
        "\n".join(
            [
                "from models.user import User",
                "",
                "class UserService:",
                "    def get_user(self):",
                "        return User()",
            ]
        ),
        encoding="utf-8",
    )
    (model_dir / "user.py").write_text(
        "class User:\n    pass\n",
        encoding="utf-8",
    )

    index = ProjectIndex(
        project_path=str(project_path),
        files=[
            FileInfo(
                path="main.py",
                imports=["services.user_service.UserService"],
                functions=["main"],
                has_main_guard=True,
            ),
            FileInfo(
                path="services/user_service.py",
                imports=["models.user.User"],
                classes=[ClassInfo(name="UserService", methods=["get_user"])],
            ),
            FileInfo(
                path="models/user.py",
                classes=[ClassInfo(name="User")],
            ),
        ],
    )
    graph = CodeGraph(
        project_path=str(project_path),
        nodes=[
            GraphNode(id="file:main.py", type="file", name="main.py", path="main.py"),
            GraphNode(
                id="func:main.py:main",
                type="function",
                name="main",
                path="main.py",
            ),
            GraphNode(
                id="file:services/user_service.py",
                type="file",
                name="services/user_service.py",
                path="services/user_service.py",
            ),
            GraphNode(
                id="class:services/user_service.py:UserService",
                type="class",
                name="UserService",
                path="services/user_service.py",
            ),
            GraphNode(
                id="file:models/user.py",
                type="file",
                name="models/user.py",
                path="models/user.py",
            ),
        ],
        edges=[
            GraphEdge(
                source="file:main.py",
                target="func:main.py:main",
                type="contains",
            ),
            GraphEdge(
                source="file:main.py",
                target="file:services/user_service.py",
                type="imports",
            ),
            GraphEdge(
                source="file:services/user_service.py",
                target="file:models/user.py",
                type="imports",
            ),
        ],
    )
    return project_path, index, graph
