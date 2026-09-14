from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from uuid import uuid4

from backend.app.core.errors import BackendError
from backend.app.infrastructure.artifacts import SnapshotArtifactBundle
from pycode.constants import DEFAULT_ARTIFACT_DIR, DEFAULT_GRAPH_FILE, DEFAULT_INDEX_FILE
from pycode.storage import load_graph, load_index


class AgentSnapshotWorkspace:
    """Materialize only exact Snapshot artifacts expected by the existing Agent."""

    def prepare(self, repository: Path, bundle: SnapshotArtifactBundle) -> None:
        root = repository.resolve()
        artifact_dir = root / DEFAULT_ARTIFACT_DIR
        self._ensure_directory(artifact_dir, root)

        temporary_index = artifact_dir / f".{DEFAULT_INDEX_FILE}.{uuid4()}.tmp"
        temporary_graph = artifact_dir / f".{DEFAULT_GRAPH_FILE}.{uuid4()}.tmp"
        try:
            shutil.copyfile(bundle.index_path, temporary_index)
            shutil.copyfile(bundle.graph_path, temporary_graph)
            index = load_index(temporary_index)
            graph = load_graph(temporary_graph)
            if (
                len(index.files) != bundle.summary.file_count
                or len(graph.nodes) != bundle.summary.node_count
                or len(graph.edges) != bundle.summary.edge_count
            ):
                raise BackendError(
                    "artifacts_unavailable",
                    "Snapshot artifacts are unavailable.",
                )
            os.replace(temporary_index, artifact_dir / DEFAULT_INDEX_FILE)
            os.replace(temporary_graph, artifact_dir / DEFAULT_GRAPH_FILE)
        except BackendError:
            raise
        except (OSError, ValueError, UnicodeError, KeyError, TypeError, AttributeError) as exc:
            raise BackendError(
                "artifacts_unavailable",
                "Snapshot artifacts could not be prepared for Agent execution.",
            ) from exc
        finally:
            for temporary in (temporary_index, temporary_graph):
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _ensure_directory(directory: Path, root: Path) -> None:
        try:
            directory.mkdir(exist_ok=True)
            info = directory.lstat()
        except OSError as exc:
            raise BackendError(
                "artifacts_unavailable",
                "Snapshot artifacts could not be prepared for Agent execution.",
            ) from exc
        if (
            stat.S_ISLNK(info.st_mode)
            or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or not stat.S_ISDIR(info.st_mode)
            or not directory.resolve().is_relative_to(root)
        ):
            raise BackendError(
                "unsafe_repository",
                "Agent artifact directory is unsafe.",
            )
