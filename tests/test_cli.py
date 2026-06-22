from pathlib import Path

import pytest

from pycode.cli import (
    ask_project,
    build_parser,
    explain_project_target,
    graph_project,
    impact_project_target,
    index_project,
    onboard_project,
    query_project_graph,
)
from pycode.models import GraphEdge, GraphNode


def test_graph_project_builds_and_saves_code_graph(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = _create_sample_project(tmp_path)
    output_path = project_path / ".pclens" / "code_graph.json"

    graph = graph_project(project_path)

    captured = capsys.readouterr()
    assert output_path.exists()
    assert "PyCode graph completed." in captured.out
    assert "Graph file:" in captured.out
    assert GraphNode(
        id="file:main.py",
        type="file",
        name="main.py",
        path="main.py",
    ) in graph.nodes
    assert GraphEdge(
        source="file:main.py",
        target="file:services/user_service.py",
        type="imports",
    ) in graph.edges


def test_query_project_graph_reads_graph_and_returns_imports(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = _create_sample_project(tmp_path)
    graph_project(project_path)
    capsys.readouterr()

    result = query_project_graph(project_path, "imports", "main.py")

    captured = capsys.readouterr()
    assert result == [
        GraphEdge(
            source="file:main.py",
            target="file:services/user_service.py",
            type="imports",
        )
    ]
    assert "PyCode query completed." in captured.out
    assert "file:main.py --imports--> file:services/user_service.py" in captured.out


def test_query_project_graph_returns_entry_candidates_without_target(
    tmp_path: Path,
) -> None:
    project_path = _create_sample_project(tmp_path)
    graph_project(project_path)

    result = query_project_graph(project_path, "entry")

    assert result == [
        GraphNode(
            id="file:main.py",
            type="file",
            name="main.py",
            path="main.py",
        )
    ]


def test_query_project_graph_requires_target_for_targeted_queries(
    tmp_path: Path,
) -> None:
    project_path = _create_sample_project(tmp_path)
    graph_project(project_path)

    with pytest.raises(ValueError, match="requires a target"):
        query_project_graph(project_path, "imports")


def test_build_parser_accepts_stage_two_commands() -> None:
    parser = build_parser()

    graph_args = parser.parse_args(["graph", "demo_project", "-o", "graph.json"])
    query_args = parser.parse_args(["query", "calls", "demo_project", "func:main.py:main"])

    assert graph_args.command == "graph"
    assert graph_args.project_path == Path("demo_project")
    assert graph_args.output_path == Path("graph.json")
    assert query_args.command == "query"
    assert query_args.query_type == "calls"
    assert query_args.target == "func:main.py:main"


def test_stage_three_commands_use_mock_llm_and_print_evidence(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = _create_sample_project(tmp_path)
    index_project(project_path)
    graph_project(project_path)
    capsys.readouterr()
    llm = _MockLLM()

    answer = ask_project(project_path, "这个项目的入口在哪里？", llm_client=llm)

    captured = capsys.readouterr()
    assert answer == "mock answer"
    assert "PyCode answer completed." in captured.out
    assert "Evidence:" in captured.out
    assert "- main.py" in captured.out
    assert "用户问题: 这个项目的入口在哪里？" in llm.prompts[-1]


def test_stage_three_explain_onboard_and_impact_use_mock_llm(
    tmp_path: Path,
) -> None:
    project_path = _create_sample_project(tmp_path)
    index_project(project_path)
    graph_project(project_path)
    llm = _MockLLM()

    explain_project_target(project_path, "services/user_service.py", llm_client=llm)
    onboard_project(project_path, llm_client=llm)
    impact_project_target(project_path, "services/user_service.py", llm_client=llm)

    assert len(llm.prompts) == 3
    assert "问题类型: explain" in llm.prompts[0]
    assert "问题类型: onboard" in llm.prompts[1]
    assert "问题类型: impact" in llm.prompts[2]


def test_stage_three_commands_require_generated_artifacts(tmp_path: Path) -> None:
    project_path = _create_sample_project(tmp_path)

    with pytest.raises(FileNotFoundError, match="Run `pycode index"):
        ask_project(project_path, "入口在哪里？", llm_client=_MockLLM())


def test_llm_answer_print_handles_unencodable_console_text(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_path = _create_sample_project(tmp_path)
    index_project(project_path)
    graph_project(project_path)
    capsys.readouterr()

    answer = ask_project(project_path, "入口在哪里？", llm_client=_MockLLM("uses nonbreaking hyphen ‑"))

    captured = capsys.readouterr()
    assert answer == "uses nonbreaking hyphen ‑"
    assert "PyCode answer completed." in captured.out


def test_build_parser_accepts_stage_three_commands() -> None:
    parser = build_parser()

    ask_args = parser.parse_args(["ask", "demo_project", "入口在哪里？"])
    explain_args = parser.parse_args(
        ["explain", "demo_project", "main.py", "--model", "gpt-5.5"]
    )
    onboard_args = parser.parse_args(["onboard", "demo_project"])
    impact_args = parser.parse_args(["impact", "demo_project", "main.py"])

    assert ask_args.command == "ask"
    assert ask_args.project_path == Path("demo_project")
    assert ask_args.question == "入口在哪里？"
    assert explain_args.command == "explain"
    assert explain_args.file_path == "main.py"
    assert explain_args.model == "gpt-5.5"
    assert onboard_args.command == "onboard"
    assert impact_args.command == "impact"
    assert impact_args.file_path == "main.py"


def _create_sample_project(tmp_path: Path) -> Path:
    project_path = tmp_path / "demo_project"
    service_dir = project_path / "services"
    service_dir.mkdir(parents=True)

    (project_path / "main.py").write_text(
        "\n".join(
            [
                "from services.user_service import UserService",
                "",
                "def main():",
                "    service = UserService()",
                "    service.get_user()",
                "",
                "if __name__ == '__main__':",
                "    main()",
            ]
        ),
        encoding="utf-8",
    )
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
    return project_path


class _MockLLM:
    def __init__(self, answer: str = "mock answer") -> None:
        self.prompts: list[str] = []
        self.answer = answer

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.answer
