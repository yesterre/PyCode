from collections import Counter
from uuid import UUID, uuid4

import pytest

from backend.app.integrations.pycode import PyCodeAdapter
from pycode.chunks import SnapshotBinding, load_chunk_index
from pycode.engine import PyCodeEngine


@pytest.mark.parametrize("mode", ["plain", "snapshot", "backfill"])
def test_adapter_enables_metadata_only_when_needed_and_parses_once(tmp_path, monkeypatch, mode):
    import pycode.parser as parser

    root = tmp_path / "repository"
    root.mkdir()
    (root / "first.py").write_text("def first():\n    return 1\n", encoding="utf-8")
    (root / "second.py").write_text("value = 2\n", encoding="utf-8")
    out = tmp_path / "artifacts"
    binding = SnapshotBinding(str(uuid4()), str(uuid4()), "a" * 40)
    requested, captured, calls = [], [], Counter()
    original_parse, original_collect = parser.ast.parse, parser.collect_chunk_spans

    class RecordingEngine(PyCodeEngine):
        def build_index(self, project_path, *, include_chunk_metadata=False):
            requested.append(include_chunk_metadata)
            index = super().build_index(project_path, include_chunk_metadata=include_chunk_metadata)
            captured.append(index)
            return index

    def parse(source, filename="<unknown>", *args, **kwargs):
        calls[filename] += 1
        return original_parse(source, filename, *args, **kwargs)

    def collect(*args, **kwargs):
        assert mode != "plain", "Plain Adapter unexpectedly collected Chunk metadata"
        return original_collect(*args, **kwargs)

    monkeypatch.setattr(parser.ast, "parse", parse)
    monkeypatch.setattr(parser, "collect_chunk_spans", collect)
    adapter = PyCodeAdapter(engine=RecordingEngine())
    identity = dict(project_id=UUID(binding.project_id), snapshot_id=UUID(binding.snapshot_id),
                    commit_sha=binding.commit_sha)
    if mode == "backfill":
        adapter.build_chunks(root, out / "chunks.json", **identity)
        assert not (out / "index.json").exists() and not (out / "graph.json").exists()
    else:
        adapter.index(root, index_path=out / "index.json", graph_path=out / "graph.json",
                      **(identity if mode == "snapshot" else {}))
        assert (out / "index.json").is_file() and (out / "graph.json").is_file()
    assert requested == [mode != "plain"]
    assert calls == {str(root / "first.py"): 1, str(root / "second.py"): 1}
    assert all((file.source_text is not None) == (mode != "plain") for file in captured[0].files)
    if mode == "plain":
        assert not (out / "chunks.json").exists()
        assert all(file.chunk_spans == [] for file in captured[0].files)
    else:
        assert len(load_chunk_index(out / "chunks.json", binding).files) == 2
