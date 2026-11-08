from collections.abc import Callable
from inspect import signature
from pathlib import Path
from contextlib import contextmanager
from uuid import UUID

from backend.app.core.errors import BackendError
from backend.app.core.models import Analysis, IndexSummary
from pycode import AnswerResult, PyCodeEngine
from pycode.agent import AgentResult
from pycode.constants import DEFAULT_ARTIFACT_DIR, DEFAULT_GRAPH_FILE, DEFAULT_INDEX_FILE
from pycode.llm_client import (
    LLMClient, LLMError, LLM_ERROR_DEPENDENCY_MISSING,
    LLM_ERROR_MISSING_CONFIG, LLM_ERROR_TIMEOUT, LLM_ERROR_UNSUPPORTED_API_TYPE,
)
from pycode.storage import load_graph, load_index
from pycode.chunks import SnapshotBinding, build_chunk_index, save_chunk_index
from pycode.retrieval_evidence import RetrievalContext
from pycode.agent.budget import (
    ExecutionBudget, ExecutionLimits, ExecutionStopped,
    budget_event, current_budget,
)


class PyCodeAdapter:
    """Convert Core results/errors without leaking presentation concerns to Core."""

    index_artifact_path = f"{DEFAULT_ARTIFACT_DIR}/{DEFAULT_INDEX_FILE}"
    graph_artifact_path = f"{DEFAULT_ARTIFACT_DIR}/{DEFAULT_GRAPH_FILE}"

    @contextmanager
    def execution_scope(self, retrieval_context, checkpoint, observer=None, *, run_id=None):
        from pycode.agent.memory_record import MemoryScope
        memory_scope = MemoryScope(retrieval_context.root, retrieval_context.binding,
                                   str(run_id) if run_id is not None else None)
        budget = ExecutionBudget(ExecutionLimits.from_environment(),
            binding=retrieval_context.binding, checkpoint=checkpoint)
        budget.retrieval_context = retrieval_context
        def observe():
            if observer is not None:
                try:
                    observer('budget', budget_event(budget))
                except Exception:
                    pass  # Observability is never the execution control channel.
        with budget.activate(), memory_scope.activate():
            try:
                observe()
                budget.check()
                yield budget
                if budget.ownership_error is not None:
                    raise budget.ownership_error
                if budget.stop_reason is None:
                    budget.check()
            except ExecutionStopped:
                if isinstance(budget.ownership_error, BackendError):
                    raise budget.ownership_error
                raise BackendError('agent_run_failed', 'Agent execution stopped: ' + str(budget.stop_reason)) from None
            finally:
                observe()

    @staticmethod
    def record_budget(result):
        if (budget := current_budget()) is not None and getattr(result, 'trace', None) is not None:
            result.trace.events.append(budget_event(budget))

    def __init__(
        self, *, engine: PyCodeEngine | None = None, llm_client: LLMClient | None = None,
        llm_client_factory: Callable[[str | None], LLMClient] | None = None,
    ) -> None:
        if llm_client is not None and llm_client_factory is not None:
            raise ValueError("Use either an LLM client or an LLM client factory.")
        self.engine = engine if engine is not None else PyCodeEngine()
        self.llm_client = llm_client
        self.llm_client_factory = llm_client_factory

    def index(
        self, root: Path, *, index_path: Path | None = None,
        graph_path: Path | None = None,
        project_id: UUID | None = None, snapshot_id: UUID | None = None,
        commit_sha: str | None = None,
    ) -> IndexSummary:
        if index_path is None or graph_path is None:
            if index_path is not None or graph_path is not None:
                raise ValueError("Both Snapshot artifact paths are required.")
            index_path, graph_path = self._artifact_paths(root)
        identity = (project_id, snapshot_id, commit_sha)
        if any(value is not None for value in identity) and any(value is None for value in identity):
            raise ValueError("A complete Project/Snapshot/Commit binding is required.")
        binding = SnapshotBinding(str(project_id), str(snapshot_id), commit_sha) if project_id is not None else None
        index = self.engine.index(
            root, output_path=index_path, graph_output_path=graph_path,
            chunk_output_path=index_path.parent / "chunks.json" if binding is not None else None,
            binding=binding,
        )
        graph = load_graph(graph_path)
        return IndexSummary(len(index.files), len(graph.nodes), len(graph.edges))

    def build_chunks(
        self, root: Path, output_path: Path, *, project_id: UUID,
        snapshot_id: UUID, commit_sha: str,
    ) -> None:
        """Backfill only the missing derivative; never rebuild valid base data."""
        index = self.engine.build_index(root, include_chunk_metadata=True)
        binding = SnapshotBinding(str(project_id), str(snapshot_id), commit_sha)
        save_chunk_index(build_chunk_index(index, binding), output_path)

    def ask(
        self, root: Path, question: str, model: str | None, *,
        index_path: Path | None = None, graph_path: Path | None = None,
        retrieval_context: RetrievalContext | None = None,
    ) -> Analysis:
        if retrieval_context is None:
            index_path, graph_path = self._check_artifacts(root, index_path, graph_path)
        try:
            result = self.engine.ask(
                root, question, model=model, llm_client=self._llm_client(model),
                index_path=index_path, graph_path=graph_path,
                **({"retrieval_context": retrieval_context} if retrieval_context is not None else {}),
            )
        except LLMError as exc:
            raise _model_error(exc) from exc
        return _analysis(result)

    def impact(
        self, root: Path, file_path: str, model: str | None, *,
        index_path: Path | None = None, graph_path: Path | None = None,
        retrieval_context: RetrievalContext | None = None,
    ) -> Analysis:
        if retrieval_context is None:
            index_path, graph_path = self._check_artifacts(root, index_path, graph_path)
        try:
            result = self.engine.impact(
                root, file_path, model=model, llm_client=self._llm_client(model),
                index_path=index_path, graph_path=graph_path,
                **({"retrieval_context": retrieval_context} if retrieval_context is not None else {}),
            )
        except LLMError as exc:
            raise _model_error(exc) from exc
        return _analysis(result)

    def snapshot_retrieval(self, root, snapshot, bundle, chunks) -> RetrievalContext:
        """Called after Service authorization, exact checkout and Store validation."""
        try:
            return RetrievalContext.create(
                root, load_index(bundle.index_path), load_graph(bundle.graph_path),
                binding=SnapshotBinding(str(snapshot.project_id), str(snapshot.id), snapshot.commit_sha),
                chunks=chunks,
            )
        except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError) as exc:
            raise BackendError("artifacts_unavailable", "Snapshot retrieval inputs are unavailable.") from exc

    @staticmethod
    def decide_question(question, target=None):
        from pycode.question import route_question
        return route_question(question, target)

    def prepare_question(self, question, decision, retrieval_context):
        try:
            return self.engine.prepare_question(question, decision, retrieval_context)
        except ExecutionStopped as exc:
            if isinstance(current_budget().ownership_error, BackendError):
                raise current_budget().ownership_error
            raise BackendError('agent_run_failed', 'Question preflight stopped: ' + exc.reason) from None
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise BackendError('artifacts_unavailable', 'Question preflight inputs are unavailable.') from exc

    @staticmethod
    def failed_preflight_event(summary):
        from pycode.agent.trace import TraceEvent
        from datetime import datetime, timezone
        return TraceEvent('ContextAssembled', datetime.now(timezone.utc), data={
            'included_section_names': [], 'skipped_section_names': ['evidence'], 'question_preflight': summary})

    def answer_question(self, root, question, *, decision, model=None,
                        retrieval_context, trace_observer=None, preflight=None):
        try:
            return self.engine.answer_question(root, question, decision=decision, model=model,
                llm_client=self._llm_client(model), retrieval_context=retrieval_context,
                trace_observer=trace_observer, preflight=preflight)
        except LLMError as exc:
            raise _model_error(exc) from exc

    def run_agent(self, root: Path, task: str, *, trace_observer=None, model=None,
                  retrieval_context: RetrievalContext | None = None, preflight=None) -> AgentResult:
        """Invoke the existing Agent facade without changing its runtime defaults."""
        # Legacy adapter wrappers may not forward the optional context argument.
        # The authorized execution scope remains the authoritative binding.
        if (budget := current_budget()) is not None:
            if retrieval_context is None:
                retrieval_context = budget.retrieval_context
            if retrieval_context is not None:
                budget.validate_binding(retrieval_context.binding)
        try:
            kwargs = {"llm_client": self._llm_client(model)}
            if model is not None:
                kwargs['model'] = model
            if retrieval_context is not None:
                kwargs["retrieval_context"] = retrieval_context
            if preflight is not None:
                preflight.initial_evidence.validate(retrieval_context)
                kwargs['initial_evidence'] = preflight.initial_evidence
            parameters = signature(self.engine.run_agent).parameters
            accepts_kwargs = any(p.kind == p.VAR_KEYWORD for p in parameters.values())
            if trace_observer is not None and ("trace_observer" in parameters or accepts_kwargs):
                kwargs["trace_observer"] = trace_observer
            result = self.engine.run_agent(root, task, **kwargs)
            if preflight is not None and result.trace is not None:
                result.trace.events.insert(0, preflight.event)
            return result
        except LLMError as exc:
            raise _model_error(exc) from exc

    @staticmethod
    def _artifact_paths(root: Path) -> tuple[Path, Path]:
        paths = (root / DEFAULT_ARTIFACT_DIR / DEFAULT_INDEX_FILE,
                 root / DEFAULT_ARTIFACT_DIR / DEFAULT_GRAPH_FILE)
        # Existing symlinks must not redirect HTTP-triggered artifact IO outside
        # the registered repository. CLI and Core path contracts stay unchanged.
        for path in paths:
            if not path.resolve().is_relative_to(root):
                raise BackendError("path_forbidden", "Artifact path is outside the project.")
        return paths

    def _check_artifacts(
        self, root: Path, index_path: Path | None = None,
        graph_path: Path | None = None,
    ) -> tuple[Path, Path]:
        if index_path is None or graph_path is None:
            if index_path is not None or graph_path is not None:
                raise ValueError("Both Snapshot artifact paths are required.")
            index_path, graph_path = self._artifact_paths(root)
        try:
            # Validate using Core readers before invoking the model. Keeping this
            # separate prevents model failures from being mistaken for corruption.
            load_index(index_path)
            load_graph(graph_path)
        except (FileNotFoundError, IsADirectoryError, NotADirectoryError,
                ValueError, UnicodeError, KeyError, TypeError, AttributeError) as exc:
            raise BackendError(
                "artifacts_unavailable", "Index or graph is unavailable. Rebuild the project index.",
            ) from exc
        return index_path, graph_path

    def _llm_client(self, model: str | None) -> LLMClient | None:
        return self.llm_client_factory(model) if self.llm_client_factory is not None else self.llm_client


def _analysis(result: AnswerResult) -> Analysis:
    summary = result.retrieval.summary() if hasattr(result.retrieval, "summary") else None
    return Analysis(result.answer, result.retrieval.intent, list(result.evidence), summary)


def _model_error(exc: LLMError) -> BackendError:
    if exc.category in {
        LLM_ERROR_MISSING_CONFIG, LLM_ERROR_UNSUPPORTED_API_TYPE, LLM_ERROR_DEPENDENCY_MISSING,
    }:
        return BackendError("model_unavailable", "Server model configuration is unavailable.")
    if exc.category == LLM_ERROR_TIMEOUT:
        return BackendError("model_timeout", "The model service timed out.")
    return BackendError("model_failed", "The model service request failed.")
