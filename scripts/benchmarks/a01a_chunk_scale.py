"""Offline scale measurement of the complete current repository Python source.

Capture every tracked/unignored Python file, then run the real Scanner/Engine
on that full snapshot. Historical ignored test workspaces are not source input.
Artifacts and copied input live only in a TemporaryDirectory. No services start.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import gc
import hashlib
import json
from pathlib import Path, PurePosixPath
import platform
import statistics
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter
import tracemalloc

from pycode.chunks import CHUNK_SCHEMA_VERSION, MAX_CHUNK_LINES, SnapshotBinding
from pycode.engine import PyCodeEngine
from pycode.scanner import IGNORED_DIRS, scan_python_files


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "docs_v3.0" / "v3_agent" / "A01" / "A01a" / "REPOSITORY_SCALE_MEASUREMENT.json"


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


def _source_paths(repo: Path) -> list[str]:
    paths = _git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard").decode("utf-8").split("\0")
    selected = []
    for name in sorted(set(paths)):
        if not name.endswith(".py"):
            continue
        relative = PurePosixPath(name)
        source = repo / name
        if (relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name
                or source.is_symlink() or not source.resolve().is_relative_to(repo)):
            raise ValueError("Unsafe repository source path.")
        if source.is_file():  # Deleted tracked files are absent from the current worktree.
            selected.append(name)
    if not selected:
        raise ValueError("Repository contains no tracked/unignored Python source.")
    return selected


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def measure_repository(repo: Path, *, rounds: int = 3) -> dict:
    if rounds < 1:
        raise ValueError("Measurement rounds must be positive.")
    if tracemalloc.is_tracing():
        raise ValueError("Measurement requires its own tracemalloc session.")
    repo = repo.resolve()
    paths = _source_paths(repo)
    base_head = _git(repo, "rev-parse", "HEAD").decode("ascii").strip()
    files, samples = {}, []
    with TemporaryDirectory(prefix="pycode-a01a-scale-") as temporary:
        temporary_root = Path(temporary)
        snapshot = temporary_root / "source"
        for name in paths:
            data = (repo / name).read_bytes()
            target = snapshot / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            files[name] = {"raw_bytes": len(data), "sha256": _digest(data)}
        source_digest = _digest(json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        # A synthetic identity, explicitly not a database Snapshot or a real
        # Commit claiming to include the uncommitted optimization changes.
        binding = SnapshotBinding("00000000-0000-0000-0000-000000000001",
                                  "00000000-0000-0000-0000-000000000002",
                                  "synthetic-working-tree:" + source_digest)
        scanned = scan_python_files(snapshot)
        scanned_paths = sorted(path.relative_to(snapshot).as_posix() for path in scanned)
        excluded_paths = [name for name in paths if set(PurePosixPath(name).parts) & IGNORED_DIRS]
        if set(scanned_paths) != set(paths) - set(excluded_paths):
            raise ValueError("Scanner input differs from the complete repository source inventory and its existing exclusions.")
        total_bytes = sum(files[name]["raw_bytes"] for name in scanned_paths)
        for number in range(1, rounds + 1):
            destination = temporary_root / f"round-{number}"
            index_path, graph_path, chunks_path = (destination / name for name in ("index.json", "graph.json", "chunks.json"))
            gc.collect()
            tracemalloc.start()
            try:
                started = perf_counter()
                index = PyCodeEngine().index(snapshot, index_path, graph_output_path=graph_path,
                                             chunk_output_path=chunks_path, binding=binding)
                elapsed = perf_counter() - started
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
            # Read-back and report construction are outside build time/memory.
            chunk_count = len(json.loads(chunks_path.read_text(encoding="utf-8"))["chunks"])
            if len(index.files) != len(scanned_paths):
                raise ValueError("Build did not include the complete source inventory.")
            samples.append({
                "round": number, "python_files": len(index.files), "source_raw_bytes": total_bytes,
                "chunks": chunk_count, "index_bytes": index_path.stat().st_size,
                "graph_bytes": graph_path.stat().st_size, "chunks_bytes": chunks_path.stat().st_size,
                "build_seconds": elapsed, "tracemalloc_python_peak_bytes": peak,
                "artifact_sha256": {name: _digest(path.read_bytes()) for name, path in
                                    (("index", index_path), ("graph", graph_path), ("chunks", chunks_path))},
            })
            del index
        if any(sample["artifact_sha256"] != samples[0]["artifact_sha256"] for sample in samples):
            raise ValueError("Repeated builds produced different artifacts.")
        # Detect an IDE/source edit during capture/measurement rather than
        # reporting measurements against a silently changed repository.
        if _source_paths(repo) != paths or any(_digest((repo / name).read_bytes()) != value["sha256"]
                                             for name, value in files.items()):
            raise ValueError("Repository source changed during measurement; rerun against a stable worktree.")
    metrics = ("python_files", "source_raw_bytes", "chunks", "index_bytes", "graph_bytes",
               "chunks_bytes", "build_seconds", "tracemalloc_python_peak_bytes")
    return {
        "schema_version": 1, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "repository": str(repo), "base_git_head": base_head,
        "input_scope": "All current tracked and unignored Python files; full source snapshot, including tests/tools; no path subset.",
        "source_inventory_sha256": source_digest, "source_files": files,
        "scanned_source_paths": scanned_paths, "scanner_excluded_paths": excluded_paths,
        "binding": {"project_id": binding.project_id, "snapshot_id": binding.snapshot_id, "commit_sha": binding.commit_sha},
        "config": {"rounds": rounds, "chunk_schema_version": CHUNK_SCHEMA_VERSION,
                   "chunk_max_lines": MAX_CHUNK_LINES, "scanner_ignored_dirs": sorted(IGNORED_DIRS)},
        "environment": {"python": platform.python_version(), "os": platform.platform()},
        "method": {
            "pipeline": "Real scan_python_files -> PyCodeEngine.index -> Index/Graph/Chunk -> existing JSON writers",
            "timing": "Full Engine.index call, including scan/read/parse/build/write, with tracemalloc enabled; no warmup",
            "memory": "tracemalloc Python allocation peak, not process RSS; excludes source snapshot preparation and read-back",
            "input_consistency": "One captured full source tree for all rounds; original inventory/hashes rechecked at end",
            "cleanup": "TemporaryDirectory removes all copied source and generated artifacts before returning",
            "external_api_calls": 0, "services_started": [], "new_dependencies": [],
        },
        "rounds": samples,
        "medians": {metric: statistics.median(sample[metric] for sample in samples) for metric in metrics},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=ROOT)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    args = parser.parse_args()
    report = measure_repository(args.repo, rounds=args.rounds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"rounds": report["config"]["rounds"], "medians": report["medians"]}))


if __name__ == "__main__":
    main()
