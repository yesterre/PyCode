from pathlib import Path
import shutil
import uuid

from pycode.models import CodeGraph, FileInfo, GraphEdge, GraphNode, ProjectIndex
from pycode.storage import save_graph, save_index
from pycode.tools import ToolContext, retrieve_context


def test_retrieve_context_reuses_stage_three_impact_retrieval() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        _create_project_artifacts(project_path)

        result = retrieve_context(
            ToolContext(project_path),
            "检查 services/user_service.py 的改动影响",
            target="services/user_service.py",
            intent="impact",
        )

        assert result.ok is True
        assert result.data["intent"] == "impact"
        assert "services/user_service.py" in result.data["evidence"]
        assert result.data["items"][0]["path"] == "services/user_service.py"
        assert "class UserService" in result.data["items"][0]["snippet"]
    finally:
        _cleanup(workspace)


def test_retrieve_context_reports_missing_artifacts() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        project_path.mkdir()

        result = retrieve_context(ToolContext(project_path), "入口在哪里？")

        assert result.ok is False
        assert result.summary == "PyCode artifacts cannot be loaded."
    finally:
        _cleanup(workspace)


def test_retrieve_context_supports_entry_onboard_and_explain_intents() -> None:
    workspace = _workspace()
    try:
        project_path = workspace / "project"
        _create_project_artifacts(project_path)
        context = ToolContext(project_path)

        entry = retrieve_context(
            context,
            "\u8fd9\u4e2a\u9879\u76ee\u7684\u5165\u53e3\u5728\u54ea\uff1f",
            intent="entry",
        )
        onboard = retrieve_context(
            context,
            "\u9605\u8bfb\u987a\u5e8f\u662f\u4ec0\u4e48\uff1f",
            intent="onboard",
        )
        explain = retrieve_context(
            context,
            "\u89e3\u91ca services/user_service.py",
            target="services/user_service.py",
            intent="explain",
        )

        assert entry.ok is True
        assert entry.data["intent"] == "entry"
        assert "main.py" in entry.data["evidence"]
        assert onboard.ok is True
        assert onboard.data["intent"] == "onboard"
        assert "main.py" in [item["path"] for item in onboard.data["items"]]
        assert explain.ok is True
        assert explain.data["intent"] == "explain"
        assert explain.data["items"][0]["path"] == "services/user_service.py"
    finally:
        _cleanup(workspace)


def _create_project_artifacts(project_path: Path) -> None:
    service_dir = project_path / "services"
    service_dir.mkdir(parents=True)
    (service_dir / "user_service.py").write_text(
        "\n".join(
            [
                "class UserService:",
                "    def get_user(self):",
                "        return 'alice'",
            ]
        ),
        encoding="utf-8",
    )
    (project_path / "main.py").write_text(
        "from services.user_service import UserService\n",
        encoding="utf-8",
    )

    save_index(
        ProjectIndex(
            project_path=str(project_path),
            files=[
                FileInfo(
                    path="services/user_service.py",
                    classes=[],
                    functions=[],
                ),
                FileInfo(
                    path="main.py",
                    imports=["services.user_service.UserService"],
                ),
            ],
        ),
        project_path / ".pclens" / "index.json",
    )
    save_graph(
        CodeGraph(
            project_path=str(project_path),
            nodes=[
                GraphNode(
                    id="file:services/user_service.py",
                    type="file",
                    name="services/user_service.py",
                    path="services/user_service.py",
                ),
                GraphNode(id="file:main.py", type="file", name="main.py", path="main.py"),
            ],
            edges=[
                GraphEdge(
                    source="file:main.py",
                    target="file:services/user_service.py",
                    type="imports",
                )
            ],
        ),
        project_path / ".pclens" / "code_graph.json",
    )


def _workspace() -> Path:
    path = Path(".pytest_tmp_tools") / f"retrieve_context_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    return path.resolve()


def _cleanup(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
