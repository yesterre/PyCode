import stat
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path, PureWindowsPath
from typing import Protocol, TypeVar
from uuid import UUID

from backend.app.integrations.graph import load_graph, local_graph

from backend.app.core.errors import BackendError
from backend.app.core.models import (
    AnalysisHistoryItem, AnalysisHistoryMode, IndexSummary, Project,
    ProjectAnalysis, ProjectSnapshot, ProjectStatus,
)
from backend.app.infrastructure.artifacts import SnapshotArtifactBundle, SnapshotArtifactStore
from backend.app.infrastructure.projects import ProjectOperationLocks
from backend.app.infrastructure.repositories import RepositoryWorkspace
from backend.app.integrations.pycode import PyCodeAdapter
from backend.app.repositories.interfaces import UnitOfWork


T = TypeVar("T")
_HISTORY_MODES: dict[str, AnalysisHistoryMode] = {
    "ask": "repository",
    "impact": "impact",
    "agent": "agent",
}


class ExecutionBoundary(Protocol):
    def ensure_owned(self) -> None: ...


class ProjectService:
    def __init__(
        self, unit_of_work: UnitOfWork, adapter: PyCodeAdapter, *,
        workspace: RepositoryWorkspace, operations: ProjectOperationLocks,
        artifacts: SnapshotArtifactStore,
    ) -> None:
        self.uow = unit_of_work
        self.adapter = adapter
        self.workspace = workspace
        self.operations = operations
        self.artifacts = artifacts

    def create(self, name: str, repo_url: str, branch: str | None = None) -> Project:
        self.workspace.validate_source(repo_url, branch)
        with self.uow.transaction():
            return self.uow.projects.create(name, repo_url, branch)

    def get(self, project_id: UUID) -> Project:
        with self.uow.transaction():
            return self._with_summary(self.uow.projects.get(project_id))

    def list_page(self, limit: int, offset: int) -> tuple[list[Project], bool]:
        with self.uow.transaction():
            page = self.uow.projects.list_page(limit + 1, offset)
            return [self._with_summary(project) for project in page[:limit]], len(page) > limit

    def create_owned(
        self, owner_id: UUID, name: str, repo_url: str, branch: str | None = None,
    ) -> Project:
        if owner_id is None:
            raise BackendError("unauthenticated", "Authentication failed.")
        self.workspace.validate_source(repo_url, branch)
        with self.uow.transaction():
            return self.uow.projects.create(name, repo_url, branch, owner_id=owner_id)

    def get_owned(self, project_id: UUID, owner_id: UUID) -> Project:
        """User-facing ownership boundary; authorize before reading derived metadata."""
        with self.uow.transaction():
            return self._with_summary(self.uow.projects.get_owned(project_id, owner_id))

    def list_owned_page(
        self, owner_id: UUID, limit: int, offset: int, *, archived: bool | None = False,
    ) -> tuple[list[Project], bool]:
        with self.uow.transaction():
            page = self.uow.projects.list_owned_page(owner_id, limit + 1, offset, archived=archived)
            return [self._with_summary(project) for project in page[:limit]], len(page) > limit

    def set_archived(self, project_id: UUID, owner_id: UUID, *, archived: bool) -> Project:
        with self.uow.transaction():
            project = self.uow.projects.set_archived(project_id, owner_id, archived=archived)
            return self._with_summary(project)

    def list_snapshots(self, project_id: UUID, limit: int, *, owner_id: UUID) -> list[ProjectSnapshot]:
        with self.uow.transaction():
            self.uow.projects.get_owned(project_id, owner_id)
            return self.uow.snapshots.list_recent(project_id, limit)

    def get_analysis_history(
        self, project_id: UUID, limit: int, offset: int, *, owner_id: UUID,
    ) -> tuple[list[AnalysisHistoryItem], bool]:
        with self.uow.transaction():
            self.uow.projects.get_owned(project_id, owner_id)
            page = self.uow.agent_runs.list_history_for_project(
                project_id, limit + 1, offset,
            )
        items = [
            AnalysisHistoryItem(
                id=run.id,
                created_at=run.created_at,
                mode=_HISTORY_MODES[run.run_type],
                question=run.task,
                answer=run.result,
            )
            for run in page[:limit]
            if run.result is not None and run.run_type in _HISTORY_MODES
        ]
        return items, len(page) > limit

    def get_snapshot(self, project_id: UUID, snapshot_id: UUID, *, owner_id: UUID) -> ProjectSnapshot:
        with self.uow.transaction():
            self.uow.projects.get_owned(project_id, owner_id)
            return self.uow.snapshots.get_for_project(project_id, snapshot_id)

    def get_graph(self, project_id: UUID, snapshot_id: UUID, *, owner_id: UUID,
                  q: str | None, node_type: str | None,
                  file_path: str | None, limit: int) -> dict:
        snapshot = self.get_snapshot(project_id, snapshot_id, owner_id=owner_id)
        if snapshot.status != "ready":
            raise BackendError("project_not_ready", "Build the project index before analysis.")
        bundle = self.artifacts.read_ready(project_id, snapshot)
        try:
            graph = load_graph(bundle.graph_path)
            return local_graph(graph, snapshot_id, q=q, node_type=node_type,
                               file_path=file_path, limit=limit)
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            raise BackendError("artifacts_unavailable", "Snapshot artifacts are unavailable.") from exc

    def index(
        self, project_id: UUID, *, task_id: UUID | None = None,
        execution_token: UUID | None = None,
        execution: ExecutionBoundary | None = None,
    ) -> Project:
        with self.uow.transaction():
            self.uow.projects.get(project_id)
        with self.operations.operation(project_id) as project_lock:
            workspace_path = self.workspace.persistent_path_for(project_id)
            with self.uow.transaction():
                project = self.uow.projects.set_status(
                    project_id, ProjectStatus.PREPARING, workspace_path=workspace_path,
                )
            target_snapshot: ProjectSnapshot | None = None
            try:
                project = self._reconcile_legacy_snapshots(project)
                root, revision = self.workspace.synchronize(project)
                self._ensure_execution(project_lock, execution)
                with self.uow.transaction():
                    project = self.uow.projects.get(project_id)
                    existing = self.uow.snapshots.get_by_commit(
                        project_id, revision.commit_sha,
                    )
                    if existing is not None:
                        self._bind_task_snapshot(
                            task_id, execution_token, existing.id,
                        )

                if existing is not None and existing.status == "ready":
                    try:
                        bundle = self.artifacts.validate(project_id, existing)
                    except BackendError as exc:
                        if exc.code not in {"artifacts_unavailable", "unsafe_repository"}:
                            raise
                    else:
                        self.workspace.checkout_exact(project, revision.commit_sha)
                        self.workspace.assert_head(project, revision.commit_sha)
                        self.workspace.protect_snapshot(
                            project, existing.id, revision.commit_sha,
                        )
                        self._ensure_execution(project_lock, execution)
                        return self._activate(
                            project_id, existing, bundle,
                            task_id=task_id, execution_token=execution_token,
                        )

                if existing is not None and existing.status != "ready":
                    try:
                        recovered = self.artifacts.recover_published(
                            project_id, existing.id, revision.commit_sha,
                        )
                    except BackendError:
                        pass
                    else:
                        self.workspace.checkout_exact(project, revision.commit_sha)
                        self.workspace.assert_head(project, revision.commit_sha)
                        self.workspace.protect_snapshot(
                            project, existing.id, revision.commit_sha,
                        )
                        self._ensure_execution(project_lock, execution)
                        return self._activate(
                            project_id, existing, recovered, finalize=True,
                            task_id=task_id, execution_token=execution_token,
                        )

                self._ensure_execution(project_lock, execution)
                with self.uow.transaction():
                    project = self.uow.projects.get(project_id)
                    if (existing is not None
                            and project.current_snapshot_id == existing.id):
                        self.uow.projects.set_current_snapshot(project_id, None)
                    target_snapshot = self.uow.snapshots.start(
                        project_id, revision.commit_sha,
                    )
                    self._bind_task_snapshot(
                        task_id, execution_token, target_snapshot.id,
                    )
                    project = self.uow.projects.set_status(
                        project_id, ProjectStatus.INDEXING,
                    )
                self.workspace.protect_snapshot(
                    project, target_snapshot.id, revision.commit_sha,
                )
                self._ensure_execution(project_lock, execution)
                root = self.workspace.checkout_exact(project, revision.commit_sha)
                self.workspace.assert_head(project, revision.commit_sha)

                def before_publish() -> None:
                    self.workspace.assert_head(project, revision.commit_sha)
                    self._ensure_execution(project_lock, execution)

                bundle = self.artifacts.build_and_publish(
                    project_id, target_snapshot.id, revision.commit_sha,
                    lambda index_path, graph_path: self.adapter.index(
                        root, index_path=index_path, graph_path=graph_path,
                    ),
                    before_publish=before_publish,
                )
                self._ensure_execution(project_lock, execution)
                return self._activate(
                    project_id, target_snapshot, bundle, finalize=True,
                    task_id=task_id, execution_token=execution_token,
                )
            except Exception as exc:
                if isinstance(exc, BackendError) and exc.code in {
                    "project_lock_lost", "worker_execution_lost",
                    "database_unavailable",
                }:
                    # A newer owner may already be operating on this Project.
                    # Do not let the old owner overwrite shared Project/Snapshot state.
                    raise
                # Keep public metadata free of raw exceptions (which may include
                # source text or credentials); the HTTP handler logs the error.
                with self.uow.transaction():
                    current = self.uow.projects.get(project_id)
                    if target_snapshot is not None:
                        self.uow.snapshots.mark_failed(
                            target_snapshot.id, _safe_index_error(exc),
                        )
                        if current.current_snapshot_id == target_snapshot.id:
                            self.uow.projects.set_current_snapshot(project_id, None)
                    self.uow.projects.set_status(
                        project_id, ProjectStatus.FAILED, error=_safe_index_error(exc),
                    )
                raise

    def recover_index_for_snapshot(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        *,
        task_id: UUID,
        execution_token: UUID,
        execution: ExecutionBoundary,
    ) -> Project:
        """Recover one durable exact Snapshot without resolving remote HEAD."""

        with self.operations.operation(project_id) as project_lock:
            target_snapshot: ProjectSnapshot | None = None
            try:
                self._ensure_execution(project_lock, execution)
                with self.uow.transaction():
                    project = self.uow.projects.get(project_id)
                    target_snapshot = self.uow.snapshots.get(snapshot_id)
                    if target_snapshot.project_id != project_id:
                        raise BackendError(
                            "database_conflict",
                            "A related persistent record is invalid.",
                        )
                    if self.uow.background_tasks.bind_snapshot_if_owner(
                        task_id, execution_token, snapshot_id,
                    ) is None:
                        raise BackendError(
                            "worker_execution_lost",
                            "Worker execution ownership was lost before completion.",
                        )
                    if target_snapshot.status == "failed":
                        raise BackendError(
                            "database_conflict",
                            "A failed Snapshot cannot be recovered automatically.",
                        )
                    if target_snapshot.status == "ready":
                        if project.current_snapshot_id != snapshot_id:
                            raise BackendError(
                                "database_conflict",
                                "The ready Snapshot is not the Project's current Snapshot.",
                            )
                    else:
                        project = self.uow.projects.set_status(
                            project_id, ProjectStatus.INDEXING,
                        )

                if target_snapshot.status == "ready":
                    bundle = self.artifacts.validate(project_id, target_snapshot)
                    return replace(project, index_summary=bundle.summary)

                if not self.workspace.commit_available(
                    project, target_snapshot.commit_sha,
                ):
                    raise BackendError(
                        "repository_commit_unavailable",
                        "The Snapshot repository commit is unavailable.",
                    )
                root = self.workspace.checkout_exact(
                    project, target_snapshot.commit_sha,
                )
                self.workspace.assert_head(project, target_snapshot.commit_sha)
                self.workspace.protect_snapshot(
                    project, target_snapshot.id, target_snapshot.commit_sha,
                )
                self._ensure_execution(project_lock, execution)

                try:
                    bundle = self.artifacts.recover_published(
                        project_id, target_snapshot.id, target_snapshot.commit_sha,
                    )
                except BackendError:
                    def before_publish() -> None:
                        self.workspace.assert_head(
                            project, target_snapshot.commit_sha,
                        )
                        self._ensure_execution(project_lock, execution)

                    bundle = self.artifacts.build_and_publish(
                        project_id, target_snapshot.id, target_snapshot.commit_sha,
                        lambda index_path, graph_path: self.adapter.index(
                            root, index_path=index_path, graph_path=graph_path,
                        ),
                        before_publish=before_publish,
                    )
                self._ensure_execution(project_lock, execution)
                return self._activate(
                    project_id, target_snapshot, bundle, finalize=True,
                    task_id=task_id, execution_token=execution_token,
                )
            except Exception as exc:
                if isinstance(exc, BackendError) and exc.code in {
                    "project_lock_lost", "worker_execution_lost",
                    "database_unavailable", "database_conflict",
                }:
                    raise
                with self.uow.transaction():
                    current = self.uow.projects.get(project_id)
                    if target_snapshot is not None:
                        self.uow.snapshots.mark_failed(
                            target_snapshot.id, _safe_index_error(exc),
                        )
                        if current.current_snapshot_id == target_snapshot.id:
                            self.uow.projects.set_current_snapshot(project_id, None)
                    self.uow.projects.set_status(
                        project_id, ProjectStatus.FAILED,
                        error=_safe_index_error(exc),
                    )
                raise

    def ask(self, project_id: UUID, question: str, model: str | None, *, owner_id: UUID) -> ProjectAnalysis:
        return self._analyze(project_id, question, model, impact=False, owner_id=owner_id)

    def impact(self, project_id: UUID, file_path: str, model: str | None, *, owner_id: UUID) -> ProjectAnalysis:
        return self._analyze(project_id, file_path, model, impact=True, owner_id=owner_id)

    def execute_on_snapshot(
        self,
        project_id: UUID,
        snapshot_id: UUID,
        operation: Callable[[Path, ProjectSnapshot, SnapshotArtifactBundle], T],
    ) -> T:
        """Run a caller operation against one persisted exact ready Snapshot."""
        with self.operations.operation(project_id) as project_lock:
            with self.uow.transaction():
                project = self.uow.projects.get(project_id)
                snapshot = self.uow.snapshots.get(snapshot_id)
                if snapshot.project_id != project_id or snapshot.status != "ready":
                    raise BackendError(
                        "project_not_ready", "Build the project index before analysis.",
                    )
            root, snapshot, bundle = self._analysis_snapshot(project, snapshot)
            project_lock.ensure_owned()
            result = operation(root, snapshot, bundle)
            project_lock.ensure_owned()
            return result

    def _analyze(
        self, project_id: UUID, text: str, model: str | None, *, impact: bool, owner_id: UUID,
    ) -> ProjectAnalysis:
        with self.uow.transaction():
            self.uow.projects.get_owned(project_id, owner_id).ensure_not_archived()
        with self.operations.operation(project_id) as project_lock:
            with self.uow.transaction():
                project = self.uow.projects.get_owned(project_id, owner_id, for_update=True)
                project.ensure_not_archived()
                if project.current_snapshot_id is None:
                    raise BackendError("project_not_ready", "Build the project index before analysis.")
                snapshot = self.uow.snapshots.get_for_project(project_id, project.current_snapshot_id)
                if snapshot.status != "ready":
                    raise BackendError(
                        "project_not_ready", "Build the project index before analysis.",
                    )
                run = self.uow.agent_runs.create(
                    project_id, snapshot.id, task=text,
                    run_type="impact" if impact else "ask", model=model,
                )
            try:
                root, snapshot, bundle = self._analysis_snapshot(
                    project, snapshot,
                )
                project_lock.ensure_owned()
                if impact:
                    target = self._impact_target(root, text)
                    result = self.adapter.impact(
                        root, target, model,
                        index_path=bundle.index_path,
                        graph_path=bundle.graph_path,
                    )
                else:
                    result = self.adapter.ask(
                        root, text, model,
                        index_path=bundle.index_path,
                        graph_path=bundle.graph_path,
                    )
                project_lock.ensure_owned()
            except BackendError as exc:
                with self.uow.transaction():
                    if exc.code in {
                        "artifacts_unavailable", "unsafe_repository",
                        "workspace_unavailable", "repository_commit_unavailable",
                        "repository_changed",
                    }:
                        persisted = self.uow.projects.get(project_id)
                        self.uow.snapshots.mark_failed(snapshot.id, exc.message)
                        if persisted.current_snapshot_id == snapshot.id:
                            self.uow.projects.set_current_snapshot(project_id, None)
                        self.uow.projects.set_status(project_id, ProjectStatus.FAILED, error=exc.message)
                    self.uow.agent_runs.fail(run.id, exc.message)
                raise
            except Exception as exc:
                with self.uow.transaction():
                    self.uow.agent_runs.fail(run.id, _safe_analysis_error(exc))
                raise
            with self.uow.transaction():
                project_lock.ensure_owned()
                self.uow.agent_runs.complete(run.id, result.answer)
            return ProjectAnalysis(project_id, result.answer, result.intent, result.evidence)

    def _with_summary(self, project: Project) -> Project:
        if project.current_snapshot_id is None:
            return replace(project, index_summary=None)
        snapshot = self.uow.snapshots.get(project.current_snapshot_id)
        if (snapshot.status != "ready" or snapshot.project_id != project.id
                or snapshot.file_count is None or snapshot.node_count is None
                or snapshot.edge_count is None):
            return replace(project, index_summary=None)
        return replace(project, index_summary=IndexSummary(
            snapshot.file_count, snapshot.node_count, snapshot.edge_count,
        ))

    def _activate(
        self, project_id: UUID, snapshot: ProjectSnapshot,
        bundle: SnapshotArtifactBundle, *, finalize: bool = False,
        task_id: UUID | None = None, execution_token: UUID | None = None,
    ) -> Project:
        with self.uow.transaction():
            if task_id is not None and execution_token is not None:
                if self.uow.background_tasks.guard_execution(
                    task_id, execution_token,
                ) is None:
                    raise BackendError(
                        "worker_execution_lost",
                        "Worker execution ownership was lost before completion.",
                    )
            if finalize:
                snapshot = self.uow.snapshots.mark_ready(
                    snapshot.id,
                    index_artifact_path=bundle.index_artifact_path,
                    graph_artifact_path=bundle.graph_artifact_path,
                    summary=bundle.summary,
                )
            self.uow.projects.set_current_snapshot(project_id, snapshot.id)
            ready = self.uow.projects.set_status(project_id, ProjectStatus.READY)
        return replace(ready, index_summary=bundle.summary)

    def _bind_task_snapshot(
        self,
        task_id: UUID | None,
        execution_token: UUID | None,
        snapshot_id: UUID,
    ) -> None:
        if task_id is None and execution_token is None:
            return
        if task_id is None or execution_token is None:
            raise BackendError(
                "database_conflict", "A related persistent record is invalid.",
            )
        if self.uow.background_tasks.bind_snapshot_if_owner(
            task_id, execution_token, snapshot_id,
        ) is None:
            raise BackendError(
                "worker_execution_lost",
                "Worker execution ownership was lost before completion.",
            )

    @staticmethod
    def _ensure_execution(project_lock, execution: ExecutionBoundary | None) -> None:
        project_lock.ensure_owned()
        if execution is not None:
            execution.ensure_owned()

    def _analysis_snapshot(
        self, project: Project, snapshot: ProjectSnapshot,
    ) -> tuple[Path, ProjectSnapshot, SnapshotArtifactBundle]:
        root = self.workspace.require_ready(project)
        if self.artifacts.is_legacy(snapshot):
            self.workspace.assert_head(project, snapshot.commit_sha)
            bundle = self.artifacts.archive_legacy(
                project.id, snapshot, root,
            )
            self.workspace.protect_snapshot(
                project, snapshot.id, snapshot.commit_sha,
            )
            with self.uow.transaction():
                snapshot = self.uow.snapshots.mark_ready(
                    snapshot.id,
                    index_artifact_path=bundle.index_artifact_path,
                    graph_artifact_path=bundle.graph_artifact_path,
                    summary=bundle.summary,
                )
        if not self.workspace.commit_available(project, snapshot.commit_sha):
            raise BackendError(
                "repository_commit_unavailable",
                "The Snapshot repository commit is unavailable.",
            )
        root = self.workspace.checkout_exact(project, snapshot.commit_sha)
        self.workspace.assert_head(project, snapshot.commit_sha)
        bundle = self.artifacts.validate(project.id, snapshot)
        return root, snapshot, bundle

    def _reconcile_legacy_snapshots(self, project: Project) -> Project:
        if project.current_snapshot_id is None:
            return project
        with self.uow.transaction():
            snapshot = self.uow.snapshots.get(project.current_snapshot_id)
        if snapshot.project_id != project.id:
            raise BackendError(
                "database_conflict", "A related persistent record is invalid.",
            )
        if not self.artifacts.is_legacy(snapshot):
            return project
        try:
            repository = self.workspace.require_ready(project)
            head = self.workspace.resolve_head(project)
            if head != snapshot.commit_sha:
                raise BackendError(
                    "artifacts_unavailable",
                    "Legacy Snapshot artifacts are unavailable.",
                )
            bundle = self.artifacts.archive_legacy(
                project.id, snapshot, repository,
            )
            self.workspace.protect_snapshot(
                project, snapshot.id, snapshot.commit_sha,
            )
        except BackendError as exc:
            with self.uow.transaction():
                persisted = self.uow.projects.get(project.id)
                self.uow.snapshots.mark_failed(snapshot.id, exc.message)
                if persisted.current_snapshot_id == snapshot.id:
                    self.uow.projects.set_current_snapshot(project.id, None)
        else:
            with self.uow.transaction():
                self.uow.snapshots.mark_ready(
                    snapshot.id,
                    index_artifact_path=bundle.index_artifact_path,
                    graph_artifact_path=bundle.graph_artifact_path,
                    summary=bundle.summary,
                )
        with self.uow.transaction():
            return self.uow.projects.get(project.id)

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
