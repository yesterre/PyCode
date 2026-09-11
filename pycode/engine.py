"""Synchronous Python entry point, independent of CLI and presentation layers."""

from dataclasses import dataclass
from pathlib import Path

from pycode.agent import AgentResult, run_agent_task
from pycode.constants import DEFAULT_ARTIFACT_DIR, DEFAULT_GRAPH_FILE, DEFAULT_INDEX_FILE
from pycode.graph_builder import build_code_graph
from pycode.llm_client import LLMClient, OpenAIResponsesClient
from pycode.models import CodeGraph, GraphEdge, GraphNode, ProjectIndex
from pycode.parser import parse_python_file
from pycode.prompt_builder import build_code_qa_prompt
from pycode.query import (
    find_entry_candidates,
    get_file_imported_by,
    get_file_imports,
    get_function_calls,
)
from pycode.retriever import (
    RetrievalResult,
    retrieve_explain,
    retrieve_for_question,
    retrieve_impact,
    retrieve_onboard,
)
from pycode.scanner import scan_python_files
from pycode.storage import load_graph, load_index, save_graph, save_index
from pycode.tools import ToolSpec


@dataclass
class AnswerResult:
    """An answer together with the exact retrieval used to build its prompt."""

    answer: str
    retrieval: RetrievalResult

    @property
    def evidence(self) -> list[str]:
        return self.retrieval.evidence


class PyCodeEngine:
    """Stateless facade. Calls return data and never render terminal output.

    Index and graph calls write artifacts; Agent calls may update internal
    memory/task state and only run tests when explicitly allowed. No state is
    cached between calls. Concurrent writes to the same project are not managed.
    """

    def build_index(self, project_path: str | Path) -> ProjectIndex:
        """Scan and parse without writing files."""
        root = Path(project_path)
        return ProjectIndex(
            project_path=str(root),
            files=[parse_python_file(path, root) for path in scan_python_files(root)],
        )

    def index(
        self, project_path: str | Path, output_path: str | Path | None = None,
    ) -> ProjectIndex:
        """Build and save only the index; explicit output paths are cwd-relative."""
        root = Path(project_path)
        result = self.build_index(root)
        destination = (
            Path(output_path) if output_path is not None
            else root / DEFAULT_ARTIFACT_DIR / DEFAULT_INDEX_FILE
        )
        save_index(result, destination)
        return result

    def graph(
        self, project_path: str | Path, output_path: str | Path | None = None,
    ) -> CodeGraph:
        """Scan current sources and save only the graph, without requiring an index file."""
        root = Path(project_path)
        result = build_code_graph(self.build_index(root))
        destination = (
            Path(output_path) if output_path is not None
            else root / DEFAULT_ARTIFACT_DIR / DEFAULT_GRAPH_FILE
        )
        save_graph(result, destination)
        return result

    def query(
        self, project_path: str | Path, query_type: str,
        target: str | None = None, graph_path: str | Path | None = None,
    ) -> list[GraphEdge] | list[GraphNode]:
        """Query a saved graph, preserving the CLI query kinds and errors."""
        path = (
            Path(graph_path) if graph_path is not None
            else Path(project_path) / DEFAULT_ARTIFACT_DIR / DEFAULT_GRAPH_FILE
        )
        graph = load_graph(path)
        queries = {
            "imports": get_file_imports,
            "imported-by": get_file_imported_by,
            "calls": get_function_calls,
        }
        if query_type in queries:
            if target is None:
                raise ValueError(f"Query '{query_type}' requires a target argument.")
            return queries[query_type](graph, target)
        if query_type == "entry":
            return find_entry_candidates(graph)
        raise ValueError(f"Unsupported query type: {query_type}")

    def ask(
        self, project_path: str | Path, question: str,
        model: str | None = None, llm_client: LLMClient | None = None,
        *, index_path: str | Path | None = None,
        graph_path: str | Path | None = None,
    ) -> AnswerResult:
        root = Path(project_path)
        index, graph = _load_project_artifacts(root, index_path, graph_path)
        return _answer(retrieve_for_question(question, root, index, graph), model, llm_client)

    def explain(
        self, project_path: str | Path, file_path: str | Path,
        model: str | None = None, llm_client: LLMClient | None = None,
    ) -> AnswerResult:
        root = Path(project_path)
        index, graph = _load_project_artifacts(root)
        return _answer(retrieve_explain(str(file_path), root, index, graph), model, llm_client)

    def onboard(
        self, project_path: str | Path,
        model: str | None = None, llm_client: LLMClient | None = None,
    ) -> AnswerResult:
        root = Path(project_path)
        index, graph = _load_project_artifacts(root)
        return _answer(retrieve_onboard(root, index, graph), model, llm_client)

    def impact(
        self, project_path: str | Path, file_path: str | Path,
        model: str | None = None, llm_client: LLMClient | None = None,
        *, index_path: str | Path | None = None,
        graph_path: str | Path | None = None,
    ) -> AnswerResult:
        root = Path(project_path)
        index, graph = _load_project_artifacts(root, index_path, graph_path)
        return _answer(retrieve_impact(str(file_path), root, index, graph), model, llm_client)

    def run_agent(
        self, project_path: str | Path, task: str, *,
        allow_tests: bool = False, plan_only: bool = False,
        model: str | None = None, graph_path: str | Path | None = None,
        llm_client: LLMClient | None = None, tools: dict[str, ToolSpec] | None = None,
        max_steps: int = 8, use_llm_planner: bool = True,
        enable_memory: bool = True, enable_memory_extraction: bool = True,
    ) -> AgentResult:
        """Run the existing harness with a fresh task and its existing policy.

        With no injected client, model configuration follows the CLI. Only
        plan_only=True plus use_llm_planner=False avoids creating that client;
        a rule planner can still use an LLM for the final summary.
        """
        root = Path(project_path)
        client = llm_client
        if client is None and not (plan_only and not use_llm_planner):
            client = OpenAIResponsesClient(model=model)
        return run_agent_task(
            task, root, allow_tests=allow_tests,
            graph_path=_resolve_agent_graph_path(root, graph_path),
            llm_client=client, tools=tools, max_steps=max_steps,
            plan_only=plan_only, use_llm_planner=use_llm_planner,
            enable_memory=enable_memory,
            enable_memory_extraction=enable_memory_extraction,
        )


def _load_project_artifacts(
    project_path: Path, index_path: str | Path | None = None,
    graph_path: str | Path | None = None,
) -> tuple[ProjectIndex, CodeGraph]:
    index_path = (
        Path(index_path) if index_path is not None
        else project_path / DEFAULT_ARTIFACT_DIR / DEFAULT_INDEX_FILE
    )
    graph_path = (
        Path(graph_path) if graph_path is not None
        else project_path / DEFAULT_ARTIFACT_DIR / DEFAULT_GRAPH_FILE
    )
    missing = [str(path) for path in (index_path, graph_path) if not path.exists()]
    if missing:
        raise FileNotFoundError(
            "Missing PyCode artifacts: " + ", ".join(missing)
            + ". Run `pycode index <project_path>` and `pycode graph <project_path>` first."
        )
    return load_index(index_path), load_graph(graph_path)


def _answer(
    retrieval: RetrievalResult, model: str | None, llm_client: LLMClient | None,
) -> AnswerResult:
    client = llm_client if llm_client is not None else OpenAIResponsesClient(model=model)
    return AnswerResult(client.generate(build_code_qa_prompt(retrieval)), retrieval)


def _resolve_agent_graph_path(
    project_path: Path, graph_path: str | Path | None,
) -> Path | None:
    if graph_path is None:
        return None
    path = Path(graph_path)
    if path.is_absolute():
        return path
    if path.exists():
        return path.resolve()
    project_relative = project_path / path
    if project_relative.exists():
        return project_relative.resolve()
    return path
