from collections import Counter
from dataclasses import replace
import json
from pathlib import Path
from uuid import uuid4

import pytest

from pycode.chunks import (
    MAX_CHUNK_LINES, SnapshotBinding, build_chunk_index, load_chunk_index,
    save_chunk_index, validate_chunk_index,
)
from pycode.engine import PyCodeEngine
from pycode.parser import parse_python_file
from pycode.storage import load_index, save_index


FIXTURE = Path(__file__).parent / "fixtures" / "a01" / "structure.py"


@pytest.fixture
def binding():
    return SnapshotBinding(str(uuid4()), str(uuid4()), "a" * 40)


def parsed(tmp_path, source):
    (tmp_path / "source.py").write_text(source, encoding="utf-8")
    return PyCodeEngine().build_index(tmp_path, include_chunk_metadata=True)


def reconstruct(index):
    return "".join(chunk.content for chunk in sorted(index.chunks, key=lambda chunk: chunk.span.start_line))


def test_decorators_nested_duplicates_classes_and_module_code_are_complete(tmp_path, binding):
    source = FIXTURE.read_text(encoding="utf-8")
    chunks = build_chunk_index(parsed(tmp_path, source), binding)
    assert reconstruct(chunks) == source
    assert len({chunk.id for chunk in chunks.chunks}) == len(chunks.chunks)
    first = next(chunk for chunk in chunks.chunks if chunk.span.symbol_name == "outer")
    assert first.content.startswith("@decorate(\n")
    nested = [chunk for chunk in chunks.chunks if chunk.span.qualified_name == "outer.nested"]
    assert len({chunk.span.symbol_key for chunk in nested}) == 3
    assert {chunk.span.occurrence for chunk in nested} == {1, 2}
    method = next(chunk for chunk in chunks.chunks if chunk.span.qualified_name == "Container.create")
    assert method.span.symbol_kind == "method" and method.span.class_name == "Container"
    assert method.content.startswith("    @classmethod\n")
    assert any(chunk.span.qualified_name == "Container.Nested.method" for chunk in chunks.chunks)
    assert any(chunk.span.symbol_name == "conditional" for chunk in chunks.chunks)
    # One line belongs to exactly one chunk; class and outer-function parts do
    # not duplicate the implementations of their nested definitions.
    assert sum(chunk.span.end_line - chunk.span.start_line + 1 for chunk in chunks.chunks) == chunks.files[0].line_count
    assert any("TypeAlias" in chunk.content and chunk.span.symbol_kind == "module" for chunk in chunks.chunks)


def test_ids_are_repeatable_and_bound_to_project_snapshot_commit(tmp_path, binding):
    index = parsed(tmp_path, FIXTURE.read_text(encoding="utf-8"))
    first = build_chunk_index(index, binding)
    second = build_chunk_index(PyCodeEngine().build_index(tmp_path, include_chunk_metadata=True), binding)
    assert first == second
    original = {chunk.id for chunk in first.chunks}
    for changed in (replace(binding, project_id=str(uuid4())), replace(binding, snapshot_id=str(uuid4())),
                    replace(binding, commit_sha="b" * 40)):
        assert original.isdisjoint(chunk.id for chunk in build_chunk_index(index, changed).chunks)


def test_index_graph_and_chunks_parse_each_python_file_once(tmp_path, binding, monkeypatch):
    import pycode.parser
    source = FIXTURE.read_text(encoding="utf-8")
    (tmp_path / "one.py").write_text(source, encoding="utf-8")
    (tmp_path / "two.py").write_text("answer = 42\n", encoding="utf-8")
    calls = Counter()
    original = pycode.parser.ast.parse

    def parse_once(source, filename="<unknown>", *args, **kwargs):
        calls[filename] += 1
        return original(source, filename, *args, **kwargs)

    monkeypatch.setattr(pycode.parser.ast, "parse", parse_once)
    PyCodeEngine().index(tmp_path, tmp_path / "out/index.json",
                         graph_output_path=tmp_path / "out/graph.json",
                         chunk_output_path=tmp_path / "out/chunks.json", binding=binding)
    assert calls == {str(tmp_path / "one.py"): 1, str(tmp_path / "two.py"): 1}
    assert len(load_chunk_index(tmp_path / "out/chunks.json", binding).files) == 2


def test_long_function_splits_at_statements_without_losing_lines(tmp_path, binding):
    source = "def large():\n" + "".join(f"    value_{n} = {n}\n" for n in range(300)) + "    return value_299\n"
    chunks = build_chunk_index(parsed(tmp_path, source), binding)
    assert reconstruct(chunks) == source
    parts = chunks.chunks
    assert len(parts) >= 3
    assert all(chunk.span.part_count == len(parts) for chunk in parts)
    assert [chunk.span.part_index for chunk in parts] == list(range(1, len(parts) + 1))
    assert all(chunk.span.end_line - chunk.span.start_line + 1 <= MAX_CHUNK_LINES for chunk in parts)


def test_unsplittable_multiline_literal_is_preserved_and_marked(tmp_path, binding):
    source = 'def text():\n    value = """\n' + "large\n" * 200 + '"""\n    return value\n'
    chunks = build_chunk_index(parsed(tmp_path, source), binding)
    assert reconstruct(chunks) == source
    assert any(chunk.span.oversized and '"""' in chunk.content for chunk in chunks.chunks)
    assert all(chunk.span.oversized == (chunk.span.end_line - chunk.span.start_line + 1 > MAX_CHUNK_LINES)
               for chunk in chunks.chunks)


@pytest.mark.parametrize("source", ["", "\n\n", "# comment only\n", "value = '中文\u2028text'", "value = 1"])
def test_empty_and_module_only_files_round_trip(tmp_path, binding, source):
    result = build_chunk_index(parsed(tmp_path, source), binding)
    assert reconstruct(result) == source
    save_chunk_index(result, tmp_path / "chunks.json")
    assert load_chunk_index(tmp_path / "chunks.json", binding) == result


def test_empty_repository(tmp_path, binding):
    result = build_chunk_index(PyCodeEngine().build_index(tmp_path, include_chunk_metadata=True), binding)
    assert result.files == [] and result.chunks == []


def test_chunk_reader_rejects_identity_content_and_coverage_corruption(tmp_path, binding):
    result = build_chunk_index(parsed(tmp_path, "x = 1\n"), binding)
    path = tmp_path / "chunks.json"
    save_chunk_index(result, path)
    for wrong in (replace(binding, snapshot_id=str(uuid4())), replace(binding, project_id=str(uuid4())),
                  replace(binding, commit_sha="b" * 40)):
        with pytest.raises(ValueError, match="binding"):
            load_chunk_index(path, wrong)
    original = json.loads(path.read_text(encoding="utf-8"))
    for mutation in (lambda data: data.update(schema_version=99),
                     lambda data: data["chunks"][0].update(content="x = 2\n"),
                     lambda data: data["chunks"].clear(),
                     lambda data: data["chunks"].append(data["chunks"][0])):
        data = json.loads(json.dumps(original))
        mutation(data)
        path.write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(ValueError):
            load_chunk_index(path, binding)


def test_legacy_index_json_contract_is_preserved(tmp_path, binding):
    index = parsed(tmp_path, "def answer():\n    return 42\n")
    path = tmp_path / "index.json"
    save_index(index, path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "source_text" not in data["files"][0] and "chunk_spans" not in data["files"][0]
    loaded = load_index(path)
    assert loaded == index
    with pytest.raises(ValueError, match="fresh parse"):
        build_chunk_index(loaded, binding)


def test_chunk_paths_cannot_escape_root(tmp_path, binding):
    index = parsed(tmp_path, "x = 1\n")
    index.files[0].path = "../outside.py"
    with pytest.raises(ValueError, match="relative"):
        build_chunk_index(index, binding)
