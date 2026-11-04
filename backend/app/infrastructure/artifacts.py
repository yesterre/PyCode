from __future__ import annotations

import hashlib
import json
import os
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
from pycode.chunks import ChunkIndex, SnapshotBinding, load_chunk_index
from pycode.utils import normalize_path


INDEX_FILE = "index.json"
GRAPH_FILE = "code_graph.json"
MANIFEST_FILE = "manifest.json"
CHUNKS_FILE = "chunks.json"


@dataclass(frozen=True)
class SnapshotArtifactBundle:
    index_path: Path
    graph_path: Path
    index_artifact_path: str
    graph_artifact_path: str
    summary: IndexSummary
    chunks_path: Path | None = None
    binding: dict | None = None


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
        require_chunks: bool = False, preserve_base: bool = False,
    ) -> SnapshotArtifactBundle:
        artifact_root = self._artifact_root(project_id)
        artifact_root.mkdir(exist_ok=True)
        self._check_entry(artifact_root, self._project_root(project_id))
        self._recover(project_id, snapshot_id, commit_sha)
        original = self._read_bundle(project_id, snapshot_id, commit_sha) if preserve_base else None
        with TemporaryDirectory(
            prefix=f".{snapshot_id}.staging-", dir=artifact_root,
        ) as temporary:
            staging = Path(temporary) / "bundle"
            staging.mkdir()
            if original is not None:
                shutil.copyfile(original.index_path, staging / INDEX_FILE)
                shutil.copyfile(original.graph_path, staging / GRAPH_FILE)
            summary = builder(staging / INDEX_FILE, staging / GRAPH_FILE)
            verified = self._verify_artifacts(staging / INDEX_FILE, staging / GRAPH_FILE)
            if verified != summary:
                raise BackendError(
                    "artifacts_unavailable",
                    "Generated Snapshot artifacts do not match their summary.",
                )
            if require_chunks and not (staging / CHUNKS_FILE).is_file():
                raise BackendError("artifacts_unavailable", "Snapshot chunks were not generated.")
            if original is not None:
                # A derived-data upgrade does not rewrite published base bytes.
                # Older parsers may have produced different graph counts; the
                # new chunks must match the old file set, not rewrite its graph.
                shutil.copyfile(original.index_path, staging / INDEX_FILE)
                shutil.copyfile(original.graph_path, staging / GRAPH_FILE)
                summary = original.summary
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
        if snapshot.project_id != project_id:
            raise BackendError("artifacts_unavailable", "Snapshot artifacts are unavailable.")
        if not self.is_legacy(snapshot):
            return self.validate(project_id, snapshot)
        root = repository.resolve()
        if root != self._project_root(project_id) / "repo":
            raise BackendError("artifacts_unavailable", "Snapshot artifacts are unavailable.")
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
        if (snapshot.project_id != project_id or snapshot.status != "ready"
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

    def read_ready(self, project_id: UUID, snapshot: ProjectSnapshot) -> SnapshotArtifactBundle:
        """Validate a published ready bundle without recovery or mutation."""
        expected_index, expected_graph = self.relative_paths(snapshot.id)
        if (snapshot.project_id != project_id or snapshot.status != "ready"
                or snapshot.index_artifact_path != expected_index
                or snapshot.graph_artifact_path != expected_graph
                or snapshot.file_count is None or snapshot.node_count is None
                or snapshot.edge_count is None):
            raise BackendError("artifacts_unavailable", "Snapshot artifacts are unavailable.")
        bundle = self._read_bundle(project_id, snapshot.id, snapshot.commit_sha)
        if bundle.summary != IndexSummary(
            snapshot.file_count, snapshot.node_count, snapshot.edge_count,
        ):
            raise BackendError("artifacts_unavailable", "Snapshot artifacts are unavailable.")
        return bundle

    @staticmethod
    def relative_paths(snapshot_id: UUID) -> tuple[str, str]:
        prefix = Path("artifacts") / str(snapshot_id)
        return ((prefix / INDEX_FILE).as_posix(), (prefix / GRAPH_FILE).as_posix())

    def _read_bundle(
        self, project_id: UUID, snapshot_id: UUID, commit_sha: str,
    ) -> SnapshotArtifactBundle:
        directory = self._bundle_dir(project_id, snapshot_id)
        return self._read_directory(directory, project_id, snapshot_id, commit_sha)

    def _read_directory(
        self, directory: Path, project_id: UUID, snapshot_id: UUID, commit_sha: str,
    ) -> SnapshotArtifactBundle:
        try:
            self._check_tree(directory, self._project_root(project_id))
            manifest = json.loads((directory / MANIFEST_FILE).read_text(encoding="utf-8"))
            index_path, graph_path = directory / INDEX_FILE, directory / GRAPH_FILE
            version = manifest.get('version')
            if type(version) is not int or version not in {1,2}:
                raise ValueError('Unsupported Snapshot manifest version')
            chunks_path = directory / CHUNKS_FILE if version==2 else None
            chunks = self._verify_chunks(chunks_path, project_id, snapshot_id, commit_sha, index_path) if chunks_path else None
            binding = SnapshotBinding(str(project_id),str(snapshot_id),commit_sha)
            summary = self._verify_artifacts(index_path, graph_path, binding=binding, chunks=chunks)
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
            if version == 2:
                expected.update(version=2, chunks_schema_version=chunks.schema_version,
                                chunks_sha256=self._sha256(chunks_path), chunk_count=len(chunks.chunks))
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
            index_path, graph_path, index_relative, graph_relative, summary, chunks_path,
            binding=dict(project_id=str(project_id),snapshot_id=str(snapshot_id),commit_sha=commit_sha),
        )

    def read_chunks(self, project_id: UUID, snapshot: ProjectSnapshot) -> ChunkIndex | None:
        """Trusted store read; user-facing callers must authorize Project first.

        A valid v1 bundle has no chunks, and is not a corrupt Snapshot.
        """
        bundle = self.read_ready(project_id, snapshot)
        if bundle.chunks_path is None:
            return None
        return self._verify_chunks(bundle.chunks_path, project_id, snapshot.id,
                                   snapshot.commit_sha, bundle.index_path)

    @staticmethod
    def _verify_chunks(path, project_id, snapshot_id, commit_sha, index_path):
        try:
            chunks = load_chunk_index(path, SnapshotBinding(str(project_id), str(snapshot_id), commit_sha))
            expected_paths = {normalize_path(file.path) for file in load_index(index_path).files}
            if {file.path for file in chunks.files} != expected_paths:
                raise ValueError("Chunk file set differs from the base Index")
            return chunks
        except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError) as exc:
            raise BackendError("artifacts_unavailable", "Snapshot chunks are unavailable.") from exc

    def _write_manifest(
        self, directory: Path, project_id: UUID, snapshot_id: UUID,
        commit_sha: str, summary: IndexSummary,
    ) -> None:
        binding = SnapshotBinding(str(project_id),str(snapshot_id),commit_sha)
        chunks = self._verify_chunks(directory / CHUNKS_FILE,project_id,snapshot_id,commit_sha,directory / INDEX_FILE) if (directory / CHUNKS_FILE).exists() else None
        self._verify_artifacts(directory / INDEX_FILE,directory / GRAPH_FILE,binding=binding,chunks=chunks)
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
        if (directory / CHUNKS_FILE).exists():
            chunks = self._verify_chunks(directory / CHUNKS_FILE, project_id, snapshot_id,
                                         commit_sha, directory / INDEX_FILE)
            manifest.update(version=2, chunks_schema_version=chunks.schema_version,
                            chunks_sha256=self._sha256(directory / CHUNKS_FILE), chunk_count=len(chunks.chunks))
        (directory / MANIFEST_FILE).write_text(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )

    def _publish(
        self, staging: Path, project_id: UUID, snapshot_id: UUID,
        commit_sha: str,
    ) -> None:
        final = self._bundle_dir(project_id, snapshot_id)
        self._publish_directory(staging, final, self._project_root(project_id),
                                lambda: self._read_bundle(project_id, snapshot_id, commit_sha))

    def _publish_directory(self, staging: Path, final: Path, root: Path, validate) -> None:
        """Shared rename/rollback boundary for base and independently versioned artifacts."""
        self._check_tree(staging, root)
        self._check_entry(final.parent, root)
        if final.exists() or final.is_symlink():
            self._check_tree(final, root)
        artifact_root = final.parent
        backup: Path | None = None
        try:
            if final.exists() or final.is_symlink():
                backup = artifact_root / f".{final.name}.backup-{uuid4()}"
                final.rename(backup)
            try:
                staging.rename(final)
                # Validate before discarding the old directory. Failed post-publish
                # checks roll back as well as Windows rename failures.
                validate()
            except Exception:
                if final.exists() and not staging.exists():
                    final.rename(staging)
                if backup is not None and backup.exists() and not final.exists():
                    backup.rename(final)
                raise
        except OSError as exc:
            raise BackendError(
                "artifact_publish_failed", "Snapshot artifacts could not be published.",
            ) from exc
        if backup is not None:
            self._remove_tree_best_effort(backup, root)

    def semantic_directory(self, target, *, create=False) -> Path:
        """Resolve every parent before writes; never accept raw client path components."""
        project_id = UUID(target.binding.project_id)
        root = self._project_root(project_id)
        # UUID + fingerprint layouts exceed legacy MAX_PATH in nested workspaces.
        path = Path('\\\\?\\' + str(root)) if os.name == 'nt' and not str(root).startswith('\\\\?\\') else root
        for name in ('artifacts', 'semantic', target.binding.snapshot_id, target.fingerprint):
            path = path / name
            if path.exists() or path.is_symlink():
                self._check_entry(path, root)
            if create:
                path.mkdir(exist_ok=True)
                self._check_entry(path, root)
        return path

    def read_semantic(self, target, chunks) -> dict:
        from pycode.semantic_index import read_artifact
        directory = self.semantic_directory(target) / 'ready'
        try:
            self._check_tree(directory, self._project_root(UUID(target.binding.project_id)))
            return read_artifact(directory, target, chunks)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise BackendError('artifacts_unavailable', 'Semantic artifacts are unavailable.') from None

    def read_vector_index(self, target, chunks):
        """Query-only reader. Never recover, create or publish semantic data."""
        from pycode.semantic_search import ReadOnlyVectorIndex
        directory = self.semantic_directory(target) / 'ready'
        try:
            self._check_tree(directory, self._project_root(UUID(target.binding.project_id)))
            return ReadOnlyVectorIndex.load(directory, target, chunks)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            raise BackendError('vector_artifact_unavailable', 'Vector query artifacts are unavailable.') from None

    def publish_semantic(self, staging: Path, target, chunks, *, before_publish) -> dict:
        from pycode.semantic_index import read_artifact
        parent = self.semantic_directory(target, create=True)
        root = self._project_root(UUID(target.binding.project_id))
        self._check_tree(staging, root)
        if staging.parent != parent:
            raise BackendError('path_forbidden', 'Semantic staging path is invalid.')
        read_artifact(staging, target, chunks)
        before_publish()
        self._publish_directory(staging, parent / 'ready', root,
                                lambda: self.read_semantic(target, chunks))
        return self.read_semantic(target, chunks)

    def recover_semantic(self, target, chunks, *, before_publish=lambda: None) -> dict | None:
        from pycode.semantic_index import read_artifact
        parent = self.semantic_directory(target)
        if not parent.exists():
            return None
        root = self._project_root(UUID(target.binding.project_id))
        self._check_tree(parent, root)
        try:
            return self.read_semantic(target, chunks)
        except BackendError:
            pass
        for backup in sorted(parent.glob('.ready.backup-*'), reverse=True):
            try:
                read_artifact(backup, target, chunks)
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
            before_publish()
            self._publish_directory(backup, parent / 'ready', root,
                                    lambda: self.read_semantic(target, chunks))
            return self.read_semantic(target, chunks)
        return None

    def _recover(
        self, project_id: UUID, snapshot_id: UUID, commit_sha: str,
    ) -> None:
        artifact_root = self._artifact_root(project_id)
        # Recovery can rename backups; reject directory aliases before any write.
        self._check_entry(artifact_root, self._project_root(project_id))
        final = self._bundle_dir(project_id, snapshot_id)
        backups = sorted(
            artifact_root.glob(f".{snapshot_id}.backup-*"),
            key=lambda path: path.name,
            reverse=True,
        ) if artifact_root.exists() else []
        if not final.exists() and backups:
            self._read_directory(backups[0], project_id, snapshot_id, commit_sha)
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
                # An interrupted replacement may leave a bad new directory and
                # a complete backup. Validate the backup before touching either.
                for backup in backups:
                    try:
                        self._read_directory(backup, project_id, snapshot_id, commit_sha)
                    except BackendError:
                        continue
                    self._check_tree(final, self._project_root(project_id))
                    damaged = artifact_root / f".{snapshot_id}.damaged-{uuid4()}"
                    try:
                        final.rename(damaged)
                        try:
                            backup.rename(final)
                        except OSError:
                            damaged.rename(final)
                            raise
                    except OSError as exc:
                        raise BackendError("artifact_publish_failed", "Snapshot artifact recovery failed.") from exc
                    self._remove_tree_best_effort(damaged, self._project_root(project_id))
                    self._recover(project_id, snapshot_id, commit_sha)
                    break
                return
            for backup in backups:
                self._remove_tree_best_effort(backup, self._project_root(project_id))

    def _artifact_root(self, project_id: UUID) -> Path:
        return self._project_root(project_id) / "artifacts"

    def _bundle_dir(self, project_id: UUID, snapshot_id: UUID) -> Path:
        return self._artifact_root(project_id) / str(snapshot_id)

    def _project_root(self, project_id: UUID) -> Path:
        # Preserve the directory entry so lstat can reject an in-root alias too.
        project_root = self.root / str(project_id)
        if not project_root.resolve().is_relative_to(self.root):
            raise BackendError("path_forbidden", "Artifact path is outside the project.")
        if not project_root.is_dir():
            raise BackendError("workspace_unavailable", "Project workspace is unavailable.")
        self._check_entry(project_root, self.root)
        return project_root

    @staticmethod
    def _verify_artifacts(index_path: Path, graph_path: Path, *, binding=None, chunks=None) -> IndexSummary:
        try:
            index = load_index(index_path)
            graph = load_graph(graph_path)
            from pycode.graph_schema import validate_graph
            validate_graph(graph,binding,chunks)
            if graph.schema_version==2 and {f['path'] for f in graph.files}!={normalize_path(f.path) for f in index.files}:
                raise ValueError('Graph and Index file sets differ')
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
                or not Path(str(path.resolve()).removeprefix('\\\\?\\')).is_relative_to(
                    Path(str(root.resolve()).removeprefix('\\\\?\\')))
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
