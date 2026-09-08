import json
import subprocess
import sys
import textwrap
from dataclasses import asdict
from pathlib import Path

import pytest

from pycode import AnswerResult, PyCodeEngine
from pycode.llm_client import LLMError
from pycode.storage import load_graph, load_index
from pycode.tools import ToolSpec
from pycode.tools.base import failure, success


class FakeLLM:
    def __init__(self, answer="offline answer"):
        self.answer = answer
        self.prompts = []

    def generate(self, prompt):
        self.prompts.append(prompt)
        return self.answer


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "service.py").write_text("def serve():\n    return 42\n", encoding="utf-8")
    (root / "main.py").write_text(
        "from service import serve\ndef main():\n    return serve()\n"
        "if __name__ == '__main__':\n    main()\n", encoding="utf-8",
    )
    return root


def prepare(root):
    engine = PyCodeEngine()
    engine.index(root)
    engine.graph(root)
    return engine


def test_engine_build_save_query_and_answer_without_output(project, capsys):
    engine = PyCodeEngine()
    assert not (project / ".pclens").exists()
    index = engine.build_index(str(project))
    assert [file.path for file in index.files] == ["main.py", "service.py"]
    assert not (project / ".pclens").exists()
    assert engine.index(project) == index
    graph = engine.graph(str(project))
    assert load_index(project / ".pclens/index.json") == index
    assert load_graph(project / ".pclens/code_graph.json") == graph
    assert engine.query(project, "entry")[0].path == "main.py"
    assert engine.query(project, "imports", "main.py")[0].target == "file:service.py"
    assert engine.query(project, "imported-by", "service.py")[0].source == "file:main.py"
    assert engine.query(project, "calls", "func:main.py:main")
    client = FakeLLM()
    results = [
        engine.ask(str(project), "entry main", llm_client=client),
        engine.explain(project, Path("service.py"), llm_client=client),
        engine.onboard(project, llm_client=client),
        engine.impact(project, Path("service.py"), llm_client=client),
    ]
    for result, prompt in zip(results, client.prompts):
        assert isinstance(result, AnswerResult)
        assert result.answer == "offline answer"
        assert result.evidence == result.retrieval.evidence
        assert result.evidence
        assert result.retrieval.question in prompt
        json.dumps(asdict(result))
    assert [result.retrieval.intent for result in results] == ["entry", "explain", "onboard", "impact"]
    assert capsys.readouterr() == ("", "")


def test_custom_outputs_and_relative_project_keep_existing_semantics(project, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    engine = PyCodeEngine()
    graph = engine.graph("project", "artifacts/graph.json")
    assert graph.project_path == "project"
    assert not (project / ".pclens").exists()
    assert engine.query("project", "entry", graph_path="artifacts/graph.json")
    index = engine.index("project", "artifacts/index.json")
    assert load_index(tmp_path / "artifacts/index.json") == index
    assert not (project / "artifacts").exists()


def test_invalid_project_and_query_errors(project, tmp_path):
    engine = PyCodeEngine()
    for operation in (engine.build_index, engine.index, engine.graph):
        with pytest.raises(FileNotFoundError):
            operation(tmp_path / "absent")
        with pytest.raises(NotADirectoryError):
            operation(project / "main.py")
    engine.graph(project)
    with pytest.raises(ValueError, match="requires a target"):
        engine.query(project, "imports")
    with pytest.raises(ValueError, match="Unsupported query type"):
        engine.query(project, "unknown")


def test_missing_artifacts_do_not_build_or_call_llm(project):
    engine = PyCodeEngine()
    client = FakeLLM()
    for method, args in [(engine.ask, ("entry",)), (engine.explain, ("main.py",)),
                         (engine.onboard, ()), (engine.impact, ("main.py",))]:
        with pytest.raises(FileNotFoundError, match="Missing PyCode artifacts"):
            method(project, *args, llm_client=client)
    assert not (project / ".pclens").exists()
    assert client.prompts == []


@pytest.mark.parametrize("artifact", ["index.json", "code_graph.json"])
def test_corrupt_artifact_error_propagates(project, artifact):
    engine = prepare(project)
    (project / ".pclens" / artifact).write_text("not json", encoding="utf-8")
    client = FakeLLM()
    with pytest.raises(json.JSONDecodeError):
        engine.ask(project, "entry", llm_client=client)
    assert client.prompts == []


def test_injected_client_precedes_model_and_errors_propagate(project, monkeypatch):
    engine = prepare(project)

    def no_default_client(**kwargs):
        raise AssertionError("Explicit client must take precedence")

    class FalseyLLM(FakeLLM):
        def __bool__(self):
            return False

    class BrokenLLM:
        def generate(self, prompt):
            raise LLMError("offline timeout", category="timeout")

    monkeypatch.setattr("pycode.engine.OpenAIResponsesClient", no_default_client)
    assert engine.ask(project, "entry", model="unused", llm_client=FalseyLLM()).answer
    with pytest.raises(LLMError) as error:
        engine.ask(project, "entry", llm_client=BrokenLLM())
    assert error.value.category == "timeout"


def test_default_client_model_is_forwarded(project, monkeypatch):
    engine = prepare(project)
    models = []

    def client_factory(*, model):
        models.append(model)
        return FakeLLM()

    monkeypatch.setattr("pycode.engine.OpenAIResponsesClient", client_factory)
    assert engine.ask(project, "entry", model="test-model").answer
    assert engine.run_agent(project, "entry", model="agent-model", use_llm_planner=False,
                            enable_memory=False, enable_memory_extraction=False).answer
    assert models == ["test-model", "agent-model"]


def test_rule_plan_only_never_constructs_client_or_executes_tools(project, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("No model or tool execution in rule plan-only")

    monkeypatch.setattr("pycode.engine.OpenAIResponsesClient", forbidden)
    result = PyCodeEngine().run_agent(
        project, "entry main", plan_only=True, use_llm_planner=False,
        enable_memory=False, enable_memory_extraction=False,
        tools={"retrieve_context": ToolSpec("retrieve_context", forbidden)},
    )
    assert result.ok and result.stop_reason == "plan_only"
    assert result.tool_results == [] and result.answer is None
    assert not (project / ".pclens").exists()
    assert capsys.readouterr() == ("", "")


def test_agent_runs_are_isolated_and_return_trace_evidence(project, tmp_path, capsys):
    engine = prepare(project)
    other = tmp_path / "other"
    other.mkdir()
    (other / "app.py").write_text(
        "def start():\n    return 'other'\nif __name__ == '__main__':\n    start()\n",
        encoding="utf-8",
    )
    engine.index(other)
    engine.graph(other)
    runs = [engine.run_agent(root, "entry main", use_llm_planner=False,
                             llm_client=FakeLLM(root.name), enable_memory=False,
                             enable_memory_extraction=False) for root in (project, other)]
    assert [run.task.project_path for run in runs] == [project, other]
    assert [run.answer for run in runs] == ["project", "other"]
    for run in runs:
        assert run.trace and run.trace.events
        assert run.todos and run.context
        assert run.tool_results and run.tool_results[0].ok
        assert run.tool_results[0].data["evidence"]
    assert runs[0].trace.run_id != runs[1].trace.run_id
    assert runs[0].todos[0] is not runs[1].todos[0]
    assert runs[0].task is not runs[1].task
    assert all("main.py" not in str(result.data) for result in runs[1].tool_results)
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("allowed", [False, True])
@pytest.mark.parametrize("tool_name", ["run_tests", "external_test"])
def test_agent_policy_gates_test_execution(project, allowed, tool_name):
    calls = []

    def run_tests(context, **kwargs):
        calls.append(context.allow_tests)
        return success(tool_name, "fake tests only")

    class ActionLLM:
        def __init__(self):
            self.responses = iter([
                "invalid initial plan",
                json.dumps({"action_type": "tool_call", "tool_name": tool_name,
                            "arguments": {"test_paths": ["tests"]}, "reason": "verify"}),
                json.dumps({"action_type": "final_answer", "final_answer": "done", "reason": "done"}),
            ])

        def generate(self, prompt):
            return next(self.responses)

    result = PyCodeEngine().run_agent(
        project, "entry main", llm_client=ActionLLM(), allow_tests=allowed,
        tools={tool_name: ToolSpec(tool_name, run_tests, read_only=False)},
        enable_memory=False, enable_memory_extraction=False, max_steps=2,
    )
    assert calls == ([True] if allowed else [])
    if tool_name == "run_tests" and not allowed:
        # The LLM action validator rejects run_tests before executor dispatch.
        assert not any(item.tool == "run_tests" for item in result.tool_results)
        assert any(event.event_type == "LLMNextActionFallback" for event in result.trace.events)
        assert "tests were not allowed" in result.planner_error
        return
    observed = next(item for item in result.tool_results if item.tool == tool_name)
    assert observed.ok is allowed
    if not allowed:
        assert observed.data["denied_by"] == "policy"


def test_agent_tool_failure_and_llm_fallback_are_observable(project):
    def failing_tool(context, **kwargs):
        return failure("retrieve_context", "Cannot retrieve", "test failure")

    result = PyCodeEngine().run_agent(
        project, "entry main", llm_client=FakeLLM("invalid JSON"),
        tools={"retrieve_context": ToolSpec("retrieve_context", failing_tool)},
        enable_memory=False, enable_memory_extraction=False, max_steps=2,
    )
    assert result.planner_source == "fallback" and result.planner_error
    assert result.tool_results[0].ok is False
    assert result.observations[0].ok is False
    assert any(todo.status == "failed" for todo in result.todos)
    events = [event.event_type for event in result.trace.events]
    assert "ObservationRecorded" in events and "LLMNextActionFallback" in events
    assert len(result.turns) <= 2


@pytest.mark.parametrize("style", ["absolute", "cwd", "project", "missing"])
def test_agent_graph_path_resolution_remains_compatible(project, tmp_path, monkeypatch, style):
    engine = prepare(project)
    graph = project / ".pclens/code_graph.json"
    monkeypatch.chdir(tmp_path)
    paths = {"absolute": graph, "cwd": Path("project/.pclens/code_graph.json"),
             "project": Path(".pclens/code_graph.json"), "missing": Path("missing.json")}
    run = engine.run_agent(project, "entry", graph_path=str(paths[style]),
                           plan_only=True, use_llm_planner=False, enable_memory=False)
    assert run.task.graph_path == (Path("missing.json") if style == "missing" else graph.resolve())


def test_core_works_in_fresh_process_with_presentation_imports_blocked(project):
    script = textwrap.dedent('''
        import importlib.abc
        import sys
        from pathlib import Path
        blocked = ("pycode.cli", "pycode.rich_output", "rich", "streamlit", "ui",
                   "fastapi", "sqlalchemy", "redis", "celery")
        class BlockImports(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
                    raise AssertionError("Forbidden Core dependency: " + fullname)
        sys.meta_path.insert(0, BlockImports())
        from pycode import AnswerResult, PyCodeEngine
        class FakeLLM:
            def generate(self, prompt):
                return "offline"
        root = Path(sys.argv[1])
        engine = PyCodeEngine()
        engine.index(root)
        engine.graph(root)
        assert engine.query(root, "entry")
        answer = engine.ask(root, "entry", llm_client=FakeLLM())
        assert isinstance(answer, AnswerResult) and answer.evidence
        assert engine.impact(root, "service.py", llm_client=FakeLLM()).evidence
        run = engine.run_agent(root, "entry", use_llm_planner=False, llm_client=FakeLLM(),
                               enable_memory=False, enable_memory_extraction=False)
        assert run.trace and run.tool_results and run.answer == "offline"
        assert not any(name in sys.modules for name in blocked)
    ''')
    completed = subprocess.run([sys.executable, "-c", script, str(project)],
                               cwd=Path(__file__).resolve().parents[1],
                               capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert completed.stdout == "" and completed.stderr == ""
