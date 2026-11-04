"""Synchronous Python entry point, independent of CLI and presentation layers."""

from dataclasses import dataclass
from inspect import Parameter, signature
from pathlib import Path

from pycode.agent import AgentResult, run_agent_task
from pycode.constants import DEFAULT_ARTIFACT_DIR, DEFAULT_GRAPH_FILE, DEFAULT_INDEX_FILE
from pycode.chunks import SnapshotBinding, build_chunk_index, save_chunk_index
from pycode.graph_builder import build_code_graph
from pycode.llm_client import LLMClient, OpenAIResponsesClient
from pycode.models import CodeGraph, GraphEdge, GraphNode, ProjectIndex
from pycode.parser import parse_python_file
from pycode.prompt_builder import build_code_qa_prompt
from pycode.retrieval_evidence import RetrievalContext
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
from pycode.tools import ToolContext, ToolSpec


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

    def build_index(
        self, project_path: str | Path, *, include_chunk_metadata: bool = False,
        include_graph_metadata: bool = False,
    ) -> ProjectIndex:
        """Scan and parse without writing files."""
        root = Path(project_path)
        return ProjectIndex(
            project_path=str(root),
            files=[parse_python_file(path, root, include_chunk_metadata=include_chunk_metadata,
                                     include_graph_metadata=include_graph_metadata)
                   for path in scan_python_files(root)],
            structure_version=2 if include_graph_metadata else None,
        )

    def index(
        self, project_path: str | Path, output_path: str | Path | None = None,
        *, graph_output_path: str | Path | None = None,
        chunk_output_path: str | Path | None = None,
        binding: SnapshotBinding | None = None,
    ) -> ProjectIndex:
        """Build once; optionally save Graph/Chunk derivatives from that parse."""
        if (chunk_output_path is None) != (binding is None):
            raise ValueError("Chunk output and Snapshot binding must be supplied together.")
        root = Path(project_path)
        result = self.build_index(root, include_chunk_metadata=chunk_output_path is not None,
                                  **self._graph_metadata_kwargs(graph_output_path is not None and binding is not None))
        destination = (
            Path(output_path) if output_path is not None
            else root / DEFAULT_ARTIFACT_DIR / DEFAULT_INDEX_FILE
        )
        save_index(result, destination)
        if graph_output_path is not None:
            self.graph(root, output_path=graph_output_path, index=result, binding=binding)
        if chunk_output_path is not None:
            save_chunk_index(build_chunk_index(result, binding), Path(chunk_output_path))
        return result

    def graph(
        self, project_path: str | Path, output_path: str | Path | None = None,
        *, index: ProjectIndex | None = None, binding: SnapshotBinding | None = None,
        include_graph_metadata: bool = False,
    ) -> CodeGraph:
        """Scan current sources and save only the graph, without requiring an index file."""
        root = Path(project_path)
        if index is not None and Path(index.project_path).resolve() != root.resolve():
            raise ValueError("Graph Index belongs to another project root.")
        result = build_code_graph(index if index is not None else self.build_index(root, **self._graph_metadata_kwargs(include_graph_metadata or binding is not None)),
                                  binding=binding)
        destination = (
            Path(output_path) if output_path is not None
            else root / DEFAULT_ARTIFACT_DIR / DEFAULT_GRAPH_FILE
        )
        save_graph(result, destination)
        return result

    def _graph_metadata_kwargs(self, requested: bool) -> dict:
        """Legacy build_index overrides keep their explicit legacy capability.

        Never retry a failed parse or rebuild sources to emulate missing facts.
        """
        if not requested:
            return {}
        parameters = signature(self.build_index).parameters
        accepts = 'include_graph_metadata' in parameters or any(
            p.kind == Parameter.VAR_KEYWORD for p in parameters.values())
        return {'include_graph_metadata': True} if accepts else {}

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
        retrieval_context: RetrievalContext | None = None,
    ) -> AnswerResult:
        root = Path(project_path)
        index, graph = _retrieval_artifacts(root, index_path, graph_path, retrieval_context)
        return _answer(retrieve_for_question(question, root, index, graph, context=retrieval_context), model, llm_client)

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
        retrieval_context: RetrievalContext | None = None,
    ) -> AnswerResult:
        root = Path(project_path)
        index, graph = _retrieval_artifacts(root, index_path, graph_path, retrieval_context)
        return _answer(retrieve_impact(str(file_path), root, index, graph, context=retrieval_context), model, llm_client)

    def run_agent(
        self, project_path: str | Path, task: str, *,
        allow_tests: bool = False, plan_only: bool = False,
        model: str | None = None, graph_path: str | Path | None = None,
        llm_client: LLMClient | None = None, tools: dict[str, ToolSpec] | None = None,
        max_steps: int = 8, use_llm_planner: bool = True,
        enable_memory: bool = True, enable_memory_extraction: bool = True,
        trace_observer=None,
        retrieval_context: RetrievalContext | None = None,
    ) -> AgentResult:
        """Run the existing harness with a fresh task and its existing policy.

        With no injected client, model configuration follows the CLI. Only
        plan_only=True plus use_llm_planner=False avoids creating that client;
        a rule planner can still use an LLM for the final summary.
        """
        root = Path(project_path)
        client = llm_client
        if retrieval_context is not None and root.resolve() != retrieval_context.root:
            raise ValueError("Retrieval context belongs to another workspace.")
        if client is None and not (plan_only and not use_llm_planner):
            client = OpenAIResponsesClient(model=model)
        return run_agent_task(
            task, root, allow_tests=allow_tests,
            graph_path=_resolve_agent_graph_path(root, graph_path),
            llm_client=client, tools=tools, max_steps=max_steps,
            plan_only=plan_only, use_llm_planner=use_llm_planner,
            enable_memory=enable_memory,
            enable_memory_extraction=enable_memory_extraction,
            trace_observer=trace_observer,
            context=ToolContext(root, allow_tests=allow_tests, retrieval_context=retrieval_context),
        )


def _retrieval_artifacts(root, index_path, graph_path, context):
    if context is not None:
        if root.resolve() != context.root:
            raise ValueError("Retrieval context belongs to another workspace.")
        return context.index, context.graph
    return _load_project_artifacts(root, index_path, graph_path)


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
