import stat
from pathlib import Path, PureWindowsPath
from uuid import UUID

from backend.app.core.errors import BackendError
from backend.app.core.models import Project, ProjectAnalysis, ProjectStatus
from backend.app.infrastructure.projects import InMemoryProjectStore
from backend.app.infrastructure.repositories import RepositoryWorkspace
from backend.app.integrations.pycode import PyCodeAdapter


class ProjectService:
    def __init__(
        self, store: InMemoryProjectStore, adapter: PyCodeAdapter, *, workspace: RepositoryWorkspace,
    ) -> None:
        self.store = store
        self.adapter = adapter
        self.workspace = workspace

    def create(self, name: str, repo_url: str, branch: str | None = None) -> Project:
        self.workspace.validate_source(repo_url, branch)
        return self.store.create(name, repo_url, branch)

    def get(self, project_id: UUID) -> Project:
        return self.store.get(project_id)

    def index(self, project_id: UUID) -> Project:
        with self.store.operation(project_id):
            project = self.store.set_status(project_id, ProjectStatus.PREPARING)
            try:
                root = self.workspace.prepare(project)
                self.store.set_status(project_id, ProjectStatus.INDEXING)
                summary = self.adapter.index(root)
            except Exception as exc:
                # Keep public metadata free of raw exceptions (which may include
                # source text or credentials); the HTTP handler logs the error.
                self.store.set_status(
                    project_id, ProjectStatus.FAILED,
                    error=exc.message if isinstance(exc, BackendError) else
                    "Indexing failed. Fix the reported error and retry indexing.",
                )
                raise
            return self.store.set_status(project_id, ProjectStatus.READY, summary=summary)

    def ask(self, project_id: UUID, question: str, model: str | None) -> ProjectAnalysis:
        return self._analyze(project_id, question, model, impact=False)

    def impact(self, project_id: UUID, file_path: str, model: str | None) -> ProjectAnalysis:
        return self._analyze(project_id, file_path, model, impact=True)

    def _analyze(
        self, project_id: UUID, text: str, model: str | None, *, impact: bool,
    ) -> ProjectAnalysis:
        with self.store.operation(project_id):
            project = self.store.get(project_id)
            if project.status != ProjectStatus.READY:
                raise BackendError("project_not_ready", "Build the project index before analysis.")
            try:
                root = self.workspace.require_ready(project.id)
                if impact:
                    target = self._impact_target(root, text)
                    result = self.adapter.impact(root, target, model)
                else:
                    result = self.adapter.ask(root, text, model)
            except BackendError as exc:
                if exc.code in {"artifacts_unavailable", "unsafe_repository", "workspace_unavailable"}:
                    self.store.set_status(project_id, ProjectStatus.FAILED, error=exc.message)
                raise
            return ProjectAnalysis(project_id, result.answer, result.intent, result.evidence)

    @staticmethod
    def _impact_target(root: Path, raw_path: str) -> str:
        # Reject drive-relative and rooted Windows paths on every host, as well
        # as native absolute paths. Normalize separators for HTTP clients.
        windows_path = PureWindowsPath(raw_path)
        path = Path(raw_path.replace("\\", "/"))
        if "\x00" in raw_path or windows_path.drive or windows_path.root or path.is_absolute():
            raise BackendError("invalid_path", "Use a project-relative Python file path.")
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            raise BackendError("path_forbidden", "Target path is outside the project.")
        if target.suffix.lower() != ".py":
            raise BackendError("invalid_path", "Impact analysis requires a Python file.")
        if not stat.S_ISREG(target.stat().st_mode):
            raise BackendError("invalid_path", "Impact target must be a regular Python file.")
        return target.relative_to(root).as_posix()
