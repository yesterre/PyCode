from pathlib import Path

from backend.app.core.errors import BackendError
from backend.app.core.models import Analysis, IndexSummary
from pycode import AnswerResult, PyCodeEngine
from pycode.constants import DEFAULT_ARTIFACT_DIR, DEFAULT_GRAPH_FILE, DEFAULT_INDEX_FILE
from pycode.llm_client import (
    LLMClient, LLMError, LLM_ERROR_DEPENDENCY_MISSING,
    LLM_ERROR_MISSING_CONFIG, LLM_ERROR_TIMEOUT, LLM_ERROR_UNSUPPORTED_API_TYPE,
)
from pycode.storage import load_graph, load_index


class PyCodeAdapter:
    """Convert Core results/errors without leaking presentation concerns to Core."""

    index_artifact_path = f"{DEFAULT_ARTIFACT_DIR}/{DEFAULT_INDEX_FILE}"
    graph_artifact_path = f"{DEFAULT_ARTIFACT_DIR}/{DEFAULT_GRAPH_FILE}"

    def __init__(
        self, *, engine: PyCodeEngine | None = None, llm_client: LLMClient | None = None,
    ) -> None:
        self.engine = engine if engine is not None else PyCodeEngine()
        self.llm_client = llm_client

    def index(self, root: Path) -> IndexSummary:
        self._artifact_paths(root)
        index = self.engine.index(root)
        graph = self.engine.graph(root)
        return IndexSummary(len(index.files), len(graph.nodes), len(graph.edges))

    def ask(self, root: Path, question: str, model: str | None) -> Analysis:
        self._check_artifacts(root)
        try:
            result = self.engine.ask(root, question, model=model, llm_client=self.llm_client)
        except LLMError as exc:
            raise _model_error(exc) from exc
        return _analysis(result)

    def impact(self, root: Path, file_path: str, model: str | None) -> Analysis:
        self._check_artifacts(root)
        try:
            result = self.engine.impact(root, file_path, model=model, llm_client=self.llm_client)
        except LLMError as exc:
            raise _model_error(exc) from exc
        return _analysis(result)

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

    def _check_artifacts(self, root: Path) -> None:
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


def _analysis(result: AnswerResult) -> Analysis:
    return Analysis(result.answer, result.retrieval.intent, list(result.evidence))


def _model_error(exc: LLMError) -> BackendError:
    if exc.category in {
        LLM_ERROR_MISSING_CONFIG, LLM_ERROR_UNSUPPORTED_API_TYPE, LLM_ERROR_DEPENDENCY_MISSING,
    }:
        return BackendError("model_unavailable", "Server model configuration is unavailable.")
    if exc.category == LLM_ERROR_TIMEOUT:
        return BackendError("model_timeout", "The model service timed out.")
    return BackendError("model_failed", "The model service request failed.")
