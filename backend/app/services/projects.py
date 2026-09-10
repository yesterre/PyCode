import stat
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from uuid import UUID

from backend.app.core.errors import BackendError
from backend.app.core.models import IndexSummary, Project, ProjectAnalysis, ProjectStatus
from backend.app.infrastructure.projects import ProjectOperationLocks
from backend.app.infrastructure.repositories import RepositoryWorkspace
from backend.app.integrations.pycode import PyCodeAdapter
from backend.app.repositories.interfaces import UnitOfWork


class ProjectService:
    def __init__(
        self, unit_of_work: UnitOfWork, adapter: PyCodeAdapter, *,
        workspace: RepositoryWorkspace, operations: ProjectOperationLocks,
    ) -> None:
        self.uow = unit_of_work
        self.adapter = adapter
        self.workspace = workspace
        self.operations = operations

    def create(self, name: str, repo_url: str, branch: str | None = None) -> Project:
        self.workspace.validate_source(repo_url, branch)
        with self.uow.transaction():
            return self.uow.projects.create(name, repo_url, branch)

    def get(self, project_id: UUID) -> Project:
        with self.uow.transaction():
            return self._with_summary(self.uow.projects.get(project_id))

    def index(self, project_id: UUID) -> Project:
        with self.uow.transaction():
            self.uow.projects.get(project_id)
        with self.operations.operation(project_id):
            workspace_path = self.workspace.persistent_path_for(project_id)
            with self.uow.transaction():
                project = self.uow.projects.set_status(
                    project_id, ProjectStatus.PREPARING, workspace_path=workspace_path,
                )
            try:
                root = self.workspace.prepare(project)
                with self.uow.transaction():
                    project = self.uow.projects.set_status(project_id, ProjectStatus.INDEXING)
                summary = self.adapter.index(root)
                commit_sha = self.workspace.resolve_head(project)
                with self.uow.transaction():
                    self.uow.snapshots.upsert(
                        project_id, commit_sha,
                        index_artifact_path=self.adapter.index_artifact_path,
                        graph_artifact_path=self.adapter.graph_artifact_path,
                        summary=summary,
                    )
                    ready = self.uow.projects.set_status(project_id, ProjectStatus.READY)
            except Exception as exc:
                # Keep public metadata free of raw exceptions (which may include
                # source text or credentials); the HTTP handler logs the error.
                with self.uow.transaction():
                    self.uow.projects.set_status(
                        project_id, ProjectStatus.FAILED, error=_safe_index_error(exc),
                    )
                raise
            return replace(ready, index_summary=summary)

    def ask(self, project_id: UUID, question: str, model: str | None) -> ProjectAnalysis:
        return self._analyze(project_id, question, model, impact=False)

    def impact(self, project_id: UUID, file_path: str, model: str | None) -> ProjectAnalysis:
        return self._analyze(project_id, file_path, model, impact=True)

    def _analyze(
        self, project_id: UUID, text: str, model: str | None, *, impact: bool,
    ) -> ProjectAnalysis:
        with self.uow.transaction():
            self.uow.projects.get(project_id)
        with self.operations.operation(project_id):
            with self.uow.transaction():
                project = self.uow.projects.get(project_id)
                snapshot = self.uow.snapshots.latest_ready(project_id)
                if project.status != ProjectStatus.READY:
                    raise BackendError("project_not_ready", "Build the project index before analysis.")
                if snapshot is None:
                    raise BackendError("artifacts_unavailable", "Index or graph is unavailable. Rebuild the project index.")
                run = self.uow.agent_runs.create(
                    project_id, snapshot.id, task=text,
                    run_type="impact" if impact else "ask", model=model,
                )
            try:
                root = self.workspace.require_ready(project)
                if impact:
                    target = self._impact_target(root, text)
                    result = self.adapter.impact(root, target, model)
                else:
                    result = self.adapter.ask(root, text, model)
            except BackendError as exc:
                with self.uow.transaction():
                    if exc.code in {"artifacts_unavailable", "unsafe_repository", "workspace_unavailable"}:
                        self.uow.projects.set_status(project_id, ProjectStatus.FAILED, error=exc.message)
                    self.uow.agent_runs.fail(run.id, exc.message)
                raise
            except Exception as exc:
                with self.uow.transaction():
                    self.uow.agent_runs.fail(run.id, _safe_analysis_error(exc))
                raise
            with self.uow.transaction():
                self.uow.agent_runs.complete(run.id, result.answer)
            return ProjectAnalysis(project_id, result.answer, result.intent, result.evidence)

    def _with_summary(self, project: Project) -> Project:
        if project.status != ProjectStatus.READY:
            return replace(project, index_summary=None)
        snapshot = self.uow.snapshots.latest_ready(project.id)
        if snapshot is None:
            return replace(project, index_summary=None)
        return replace(project, index_summary=IndexSummary(
            snapshot.file_count, snapshot.node_count, snapshot.edge_count,
        ))

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


def _safe_index_error(exc: Exception) -> str:
    if isinstance(exc, BackendError):
        return exc.message
    return "Indexing failed. Fix the reported error and retry indexing."


def _safe_analysis_error(exc: Exception) -> str:
    if isinstance(exc, PermissionError):
        return "Project file access was denied."
    if isinstance(exc, (FileNotFoundError, NotADirectoryError)):
        return "Repository or target path does not exist."
    if isinstance(exc, (SyntaxError, UnicodeError)):
        return "Python source could not be parsed or decoded."
    return "Analysis failed."
