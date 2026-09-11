from __future__ import annotations

import hashlib
import json
import shutil
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import UUID, uuid4

from backend.app.core.errors import BackendError
from backend.app.core.models import IndexSummary, ProjectSnapshot
from pycode.storage import load_graph, load_index


INDEX_FILE = "index.json"
GRAPH_FILE = "code_graph.json"
MANIFEST_FILE = "manifest.json"


@dataclass(frozen=True)
class SnapshotArtifactBundle:
    index_path: Path
    graph_path: Path
    index_artifact_path: str
    graph_artifact_path: str
    summary: IndexSummary


class SnapshotArtifactStore:
    """Publish immutable-per-Commit analysis bundles outside the Git tree."""

    def __init__(self, workspace_root: Path) -> None:
        self.root = workspace_root.resolve()

    @staticmethod
    def is_legacy(snapshot: ProjectSnapshot) -> bool:
        return (
            snapshot.index_artifact_path == ".pclens/index.json"
            and snapshot.graph_artifact_path == ".pclens/code_graph.json"
        )

    def build_and_publish(
        self, project_id: UUID, snapshot_id: UUID, commit_sha: str,
        builder: Callable[[Path, Path], IndexSummary],
        *, before_publish: Callable[[], None] | None = None,
    ) -> SnapshotArtifactBundle:
        artifact_root = self._artifact_root(project_id)
        artifact_root.mkdir(exist_ok=True)
        self._check_entry(artifact_root, self._project_root(project_id))
        self._recover(project_id, snapshot_id, commit_sha)
        with TemporaryDirectory(
            prefix=f".{snapshot_id}.staging-", dir=artifact_root,
        ) as temporary:
            staging = Path(temporary) / "bundle"
            staging.mkdir()
            summary = builder(staging / INDEX_FILE, staging / GRAPH_FILE)
            verified = self._verify_artifacts(staging / INDEX_FILE, staging / GRAPH_FILE)
            if verified != summary:
                raise BackendError(
                    "artifacts_unavailable",
                    "Generated Snapshot artifacts do not match their summary.",
                )
            self._write_manifest(
                staging, project_id, snapshot_id, commit_sha, summary,
            )
            if before_publish is not None:
                before_publish()
            self._publish(staging, project_id, snapshot_id, commit_sha)
        return self._read_bundle(project_id, snapshot_id, commit_sha)

    def recover_published(
        self, project_id: UUID, snapshot_id: UUID, commit_sha: str,
    ) -> SnapshotArtifactBundle:
        self._recover(project_id, snapshot_id, commit_sha)
        return self._read_bundle(project_id, snapshot_id, commit_sha)

    def archive_legacy(
        self, project_id: UUID, snapshot: ProjectSnapshot, repository: Path,
    ) -> SnapshotArtifactBundle:
        if not self.is_legacy(snapshot):
            return self.validate(project_id, snapshot)
        root = repository.resolve()
        index_path = root / ".pclens" / INDEX_FILE
        graph_path = root / ".pclens" / GRAPH_FILE
        for path in (index_path, graph_path):
            self._check_entry(path, root)
        summary = self._verify_artifacts(index_path, graph_path)
        expected = IndexSummary(
            snapshot.file_count or 0, snapshot.node_count or 0, snapshot.edge_count or 0,
        )
        if summary != expected:
            raise BackendError(
                "artifacts_unavailable",
                "Legacy Snapshot artifacts do not match their persisted summary.",
            )

        def copy_legacy(target_index: Path, target_graph: Path) -> IndexSummary:
            shutil.copy2(index_path, target_index)
            shutil.copy2(graph_path, target_graph)
            return summary

        return self.build_and_publish(
            project_id, snapshot.id, snapshot.commit_sha, copy_legacy,
        )

    def validate(
        self, project_id: UUID, snapshot: ProjectSnapshot,
    ) -> SnapshotArtifactBundle:
        expected_index, expected_graph = self.relative_paths(snapshot.id)
        if (snapshot.status != "ready"
                or snapshot.index_artifact_path != expected_index
                or snapshot.graph_artifact_path != expected_graph
                or snapshot.file_count is None or snapshot.node_count is None
                or snapshot.edge_count is None):
            raise BackendError(
                "artifacts_unavailable", "Snapshot artifacts are unavailable.",
            )
        self._recover(project_id, snapshot.id, snapshot.commit_sha)
        bundle = self._read_bundle(project_id, snapshot.id, snapshot.commit_sha)
        if bundle.summary != IndexSummary(
            snapshot.file_count, snapshot.node_count, snapshot.edge_count,
        ):
            raise BackendError(
                "artifacts_unavailable",
                "Snapshot artifacts do not match their persisted summary.",
            )
        return bundle

    @staticmethod
    def relative_paths(snapshot_id: UUID) -> tuple[str, str]:
        prefix = Path("artifacts") / str(snapshot_id)
        return ((prefix / INDEX_FILE).as_posix(), (prefix / GRAPH_FILE).as_posix())

    def _read_bundle(
        self, project_id: UUID, snapshot_id: UUID, commit_sha: str,
    ) -> SnapshotArtifactBundle:
        directory = self._bundle_dir(project_id, snapshot_id)
        try:
            self._check_tree(directory, self._project_root(project_id))
            manifest = json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8"))
            index_path, graph_path = directory / INDEX_FILE, directory / GRAPH_FILE
            summary = self._verify_artifacts(index_path, graph_path)
            expected = {
                "version": 1,
                "project_id": str(project_id),
                "snapshot_id": str(snapshot_id),
                "commit_sha": commit_sha,
                "file_count": summary.file_count,
                "node_count": summary.node_count,
                "edge_count": summary.edge_count,
                "index_sha256": self._sha256(index_path),
                "graph_sha256": self._sha256(graph_path),
            }
            if manifest != expected:
                raise ValueError("Snapshot artifact manifest mismatch")
        except BackendError:
            raise
        except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError) as exc:
            raise BackendError(
                "artifacts_unavailable", "Snapshot artifacts are unavailable.",
            ) from exc
        index_relative, graph_relative = self.relative_paths(snapshot_id)
        return SnapshotArtifactBundle(
            index_path, graph_path, index_relative, graph_relative, summary,
        )

    def _write_manifest(
        self, directory: Path, project_id: UUID, snapshot_id: UUID,
        commit_sha: str, summary: IndexSummary,
    ) -> None:
        manifest = {
            "version": 1,
            "project_id": str(project_id),
            "snapshot_id": str(snapshot_id),
            "commit_sha": commit_sha,
            "file_count": summary.file_count,
            "node_count": summary.node_count,
            "edge_count": summary.edge_count,
            "index_sha256": self._sha256(directory / INDEX_FILE),
            "graph_sha256": self._sha256(directory / GRAPH_FILE),
        }
        (directory / MANIFEST_FILE).write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    def _publish(
        self, staging: Path, project_id: UUID, snapshot_id: UUID,
        commit_sha: str,
    ) -> None:
        final = self._bundle_dir(project_id, snapshot_id)
        artifact_root = final.parent
        backup: Path | None = None
        try:
            if final.exists() or final.is_symlink():
                backup = artifact_root / f".{snapshot_id}.backup-{uuid4()}"
                final.rename(backup)
            try:
                staging.rename(final)
            except OSError:
                if backup is not None and backup.exists() and not final.exists():
                    backup.rename(final)
                raise
        except OSError as exc:
            raise BackendError(
                "artifact_publish_failed", "Snapshot artifacts could not be published.",
            ) from exc
        if backup is not None:
            self._remove_tree_best_effort(backup, self._project_root(project_id))
        # Refuse to report success unless the newly published bundle is complete.
        self._read_bundle(project_id, snapshot_id, commit_sha)

    def _recover(
        self, project_id: UUID, snapshot_id: UUID, commit_sha: str,
    ) -> None:
        artifact_root = self._artifact_root(project_id)
        final = self._bundle_dir(project_id, snapshot_id)
        backups = sorted(
            artifact_root.glob(f".{snapshot_id}.backup-*"),
            key=lambda path: path.name,
            reverse=True,
        ) if artifact_root.exists() else []
        if not final.exists() and backups:
            try:
                backups[0].rename(final)
            except OSError as exc:
                raise BackendError(
                    "artifact_publish_failed",
                    "Snapshot artifact recovery failed.",
                ) from exc
            backups = backups[1:]
        if final.exists():
            try:
                self._read_bundle(project_id, snapshot_id, commit_sha)
            except BackendError:
                # A repair will replace this directory. Preserve backups until
                # that replacement succeeds.
                return
            for backup in backups:
                self._remove_tree_best_effort(backup, self._project_root(project_id))

    def _artifact_root(self, project_id: UUID) -> Path:
        return self._project_root(project_id) / "artifacts"

    def _bundle_dir(self, project_id: UUID, snapshot_id: UUID) -> Path:
        return self._artifact_root(project_id) / str(snapshot_id)

    def _project_root(self, project_id: UUID) -> Path:
        project_root = (self.root / str(project_id)).resolve()
        if not project_root.is_relative_to(self.root):
            raise BackendError("path_forbidden", "Artifact path is outside the project.")
        if not project_root.is_dir():
            raise BackendError("workspace_unavailable", "Project workspace is unavailable.")
        self._check_entry(project_root, self.root)
        return project_root

    @staticmethod
    def _verify_artifacts(index_path: Path, graph_path: Path) -> IndexSummary:
        try:
            index = load_index(index_path)
            graph = load_graph(graph_path)
        except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError) as exc:
            raise BackendError(
                "artifacts_unavailable", "Snapshot artifacts are unavailable.",
            ) from exc
        return IndexSummary(len(index.files), len(graph.nodes), len(graph.edges))

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _check_tree(cls, directory: Path, root: Path) -> None:
        cls._check_entry(directory, root)
        if not directory.is_dir():
            raise BackendError("artifacts_unavailable", "Snapshot artifact directory is invalid.")
        pending = [directory]
        while pending:
            current = pending.pop()
            for child in current.iterdir():
                cls._check_entry(child, root)
                if child.is_dir():
                    pending.append(child)

    @staticmethod
    def _check_entry(path: Path, root: Path) -> None:
        try:
            info = path.lstat()
        except OSError as exc:
            raise BackendError("artifacts_unavailable", "Snapshot artifacts are unavailable.") from exc
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                or not path.resolve().is_relative_to(root)
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                or (stat.S_ISREG(info.st_mode) and info.st_nlink > 1)):
            raise BackendError(
                "unsafe_repository", "Snapshot artifacts contain an unsafe path.",
            )

    @classmethod
    def _remove_tree_best_effort(cls, path: Path, root: Path) -> None:
        try:
            cls._check_tree(path, root)
            shutil.rmtree(path)
        except (BackendError, OSError):
            # A uniquely named backup is harmless and can be reconciled later.
            pass
