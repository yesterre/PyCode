from collections import Counter
from pathlib import Path
import socket
import statistics

import pytest

from scripts.benchmarks import a01a_chunk_scale as scale


def test_scale_runs_real_pipeline_on_full_inventory_and_cleans_artifacts(tmp_path, monkeypatch):
    root = tmp_path / "repository"
    sources = {"core.py": b"def run():\n    return 1\n",
               "tests/test_real.py": b"value = 2\n",
               "tests/fixtures/node_modules/ignored.py": b"ignored = True\n"}
    for name, source in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source)

    def git(repo, *args):
        assert repo == root
        if args == ("rev-parse", "HEAD"):
            return b"a" * 40
        assert args == ("ls-files", "-z", "--cached", "--others", "--exclude-standard")
        return "\0".join(sources).encode("utf-8") + b"\0"

    import pycode.parser as parser
    calls, temporary_paths = Counter(), []
    original_parse, original_temporary = parser.ast.parse, scale.TemporaryDirectory

    def parse(source, filename="<unknown>", *args, **kwargs):
        calls[Path(filename).name] += 1
        return original_parse(source, filename, *args, **kwargs)

    def temporary(**kwargs):
        context = original_temporary(dir=tmp_path, **kwargs)
        temporary_paths.append(Path(context.name))
        return context

    def forbidden(*args, **kwargs):
        pytest.fail("Scale measurement must remain offline")

    monkeypatch.setattr(scale, "_git", git)
    monkeypatch.setattr(scale, "TemporaryDirectory", temporary)
    monkeypatch.setattr(parser.ast, "parse", parse)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    result = scale.measure_repository(root)
    assert calls == {"core.py": 3, "test_real.py": 3}
    assert len(temporary_paths) == 1 and not temporary_paths[0].exists()
    assert result["base_git_head"] == "a" * 40
    assert set(result["source_files"]) == set(sources)
    assert result["scanned_source_paths"] == ["core.py", "tests/test_real.py"]
    assert result["scanner_excluded_paths"] == ["tests/fixtures/node_modules/ignored.py"]
    assert result["binding"]["commit_sha"].endswith(result["source_inventory_sha256"])
    for name, source in sources.items():
        assert result["source_files"][name] == {"raw_bytes": len(source), "sha256": scale._digest(source)}
        assert (root / name).read_bytes() == source
    assert len(result["rounds"]) == 3
    assert len({tuple(row["artifact_sha256"].values()) for row in result["rounds"]}) == 1
    for row in result["rounds"]:
        assert row["python_files"] == 2 and row["chunks"] == 2
        assert row["source_raw_bytes"] == sum(len(sources[name]) for name in result["scanned_source_paths"])
        for metric in ("index_bytes", "graph_bytes", "chunks_bytes", "build_seconds", "tracemalloc_python_peak_bytes"):
            assert row[metric] > 0
    for metric, median in result["medians"].items():
        assert median == statistics.median(row[metric] for row in result["rounds"])
    assert "not process RSS" in result["method"]["memory"]
    assert result["method"]["services_started"] == []


@pytest.mark.parametrize("rounds", [0, -1])
def test_scale_rejects_invalid_round_counts(tmp_path, rounds):
    with pytest.raises(ValueError, match="positive"):
        scale.measure_repository(tmp_path, rounds=rounds)
