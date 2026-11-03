"""A02a evidence invariants, entirely offline and without a query-time parser."""
from dataclasses import replace
from uuid import uuid4

import pytest

from pycode.chunks import SnapshotBinding, build_chunk_index
from pycode.engine import PyCodeEngine
from pycode.graph_builder import build_code_graph
from pycode.models import CodeGraph
from pycode.prompt_builder import build_code_qa_prompt
from pycode.retrieval_evidence import Query, RetrievalContext, RetrievalRequest, source_lines
from pycode.retriever import retrieve, retrieve_for_question, retrieve_impact
from pycode.tools import ToolContext, retrieve_context


def fixture_context(root, source, *, filename="logic.py"):
    root.mkdir(parents=True, exist_ok=True)
    (root / filename).write_text(source, encoding="utf-8")
    index = PyCodeEngine().build_index(root, include_chunk_metadata=True)
    graph = build_code_graph(index)
    binding = SnapshotBinding(str(uuid4()), str(uuid4()), "a" * 40)
    chunks = build_chunk_index(index, binding)
    return RetrievalContext.create(root, index, graph, binding=binding, chunks=chunks)


def assert_exact(item, text):
    lines = source_lines(text)
    expected = "\n".join(f"{n}: {lines[n - 1]}" for n in range(item.start_line, item.end_line + 1))
    assert item.snippet == expected
    assert item.coverage in {"complete_symbol", "complete_file"}


def test_long_decorated_definition_reconstructs_all_intersecting_chunks_without_parser(tmp_path, monkeypatch):
    source = "# header\n" * 100 + "@decorate(\n    flag=True,\n)\ndef lengthy():\n"
    source += "    value = 1\n" * 135
    source += "    def nested():\n        return 'nested-body'\n    return nested()\n"
    context = fixture_context(tmp_path, source)
    def forbidden(*args, **kwargs):
        pytest.fail("retrieval must not scan or parse Python")
    monkeypatch.setattr("ast.parse", forbidden)
    monkeypatch.setattr("pycode.scanner.scan_python_files", forbidden)
    result = retrieve(RetrievalRequest(Query("lengthy")), context)
    assert len(result.items) == 1
    item = result.items[0]
    assert item.start_line == 101 and item.end_line == len(source_lines(source))
    assert item.symbol == "lengthy" and item.coverage == "complete_symbol"
    assert len(item.chunk_ids) > 1
    assert any(chunk.id in item.chunk_ids and chunk.span.qualified_name == "lengthy.nested"
               for chunk in context.chunks.chunks)
    assert item.chunk_ids == tuple(chunk.id for chunk in context.chunks.chunks
                                  if chunk.span.start_line <= item.end_line and chunk.span.end_line >= item.start_line)
    assert_exact(item, source)
    assert "@decorate(" in item.snippet and "return nested()" in item.snippet


def test_fast_path_and_tool_are_only_projections_of_same_complete_evidence(tmp_path):
    source = "def gigantic():\n    payload = '''" + "x" * 14000 + "\n" + "literal-line\n" * 140 + "    '''\n    return payload\n"
    context = fixture_context(tmp_path, source)
    core = retrieve_for_question("gigantic", tmp_path, context.index, context.graph, context=context)
    tool = retrieve_context(ToolContext(tmp_path, retrieval_context=context), "gigantic", max_snippet_chars=10)
    assert tool.ok
    displayed = []
    for item in tool.data["items"]:
        assert item["snippet_truncated"] is False
        displayed.append({key: value for key, value in item.items() if key != "snippet_truncated"})
    assert displayed == [item.to_dict() for item in core.items]
    assert tool.data["evidence"] == core.evidence
    assert core.items[0].snippet in build_code_qa_prompt(core)
    assert "max_snippet_chars_ignored_to_preserve_evidence" in tool.data["compatibility_notes"]
    assert len(core.items[0].snippet) > 14000
    assert_exact(core.items[0], source)
    assert any(chunk.span.oversized for chunk in context.chunks.chunks)


def test_nested_qualified_symbols_repeated_definitions_and_class_coverage(tmp_path):
    source = """class Container:
    @classmethod
    def method(cls):
        def inner():
            return 1
        return inner()

def repeated():
    return 1
def repeated():
    return 2
"""
    context = fixture_context(tmp_path, source)
    method = retrieve(RetrievalRequest(Query("Container.method")), context).items
    assert len(method) == 1
    assert method[0].symbol == "Container.method" and method[0].class_name == "Container"
    assert method[0].start_line == 2 and method[0].end_line == 6
    assert "@classmethod" in method[0].snippet
    assert_exact(method[0], source)
    nested = retrieve(RetrievalRequest(Query("Container.method.inner")), context).items
    assert len(nested) == 1 and nested[0].symbol.endswith(".inner")
    repeated = retrieve(RetrievalRequest(Query("repeated")), context).items
    assert len(repeated) == 2 and len({item.symbol_key for item in repeated}) == 2
    assert repeated[0].chunk_ids != repeated[1].chunk_ids
    cls = retrieve(RetrievalRequest(Query("Container")), context).items
    assert len(cls) == 1 and cls[0].symbol_kind == "class"
    assert_exact(cls[0], source)


@pytest.mark.parametrize("field", ["project_id", "snapshot_id", "commit_sha"])
def test_context_rejects_chunk_binding_mixup(tmp_path, field):
    context = fixture_context(tmp_path, "def target():\n    return 1\n")
    value = "b" * 40 if field == "commit_sha" else str(uuid4())
    with pytest.raises(ValueError):
        RetrievalContext.create(tmp_path, context.index, context.graph,
                                binding=replace(context.binding, **{field: value}), chunks=context.chunks)


def test_published_sources_do_not_follow_later_checkout_changes(tmp_path):
    source = "def target():\n    return 'original-snapshot'\n"
    context = fixture_context(tmp_path, source)
    (tmp_path / "logic.py").write_text("def target():\n    return 'other-commit'\n", encoding="utf-8")
    result = retrieve(RetrievalRequest(Query("target")), context)
    assert_exact(result.items[0], source)
    assert result.binding == context.binding
    assert "other-commit" not in result.items[0].snippet


@pytest.mark.parametrize("overrides", [{"index_path": "other.json"}, {"graph_path": "other.json"},
                                      {"index_path": "../../other/index.json"}])
def test_tool_cannot_override_trusted_artifacts(tmp_path, overrides):
    context = fixture_context(tmp_path, "def target():\n    return 1\n")
    result = retrieve_context(ToolContext(tmp_path, retrieval_context=context), "target", **overrides)
    assert not result.ok and result.error == "trusted_artifacts_cannot_be_overridden"
    assert context.binding == context.chunks.binding


@pytest.mark.parametrize("key", ["project_id", "snapshot_id", "commit_sha", "retrieval_context"])
def test_tool_binding_is_not_an_accepted_argument(tmp_path, key):
    from pycode.tools import TOOLS
    from pycode.tools.base import validate_tool_arguments
    context = fixture_context(tmp_path, "x = 1\n")
    error = validate_tool_arguments(TOOLS["retrieve_context"], {"question": "x", key: "forged"})
    assert error is not None
    assert context.binding == context.chunks.binding


def test_empty_result_and_missing_graph_are_explicit_without_fallback(tmp_path):
    context = fixture_context(tmp_path, "def target():\n    return 'marker'\n")
    empty = retrieve(RetrievalRequest(Query("absent_identifier_98245")), context)
    assert empty.status == "empty" and empty.items == [] and empty.evidence == []
    missing = RetrievalContext.create(tmp_path, context.index, None, binding=context.binding, chunks=context.chunks)
    found = retrieve(RetrievalRequest(Query("marker")), missing)
    assert found.status == "degraded" and "graph_missing" in found.diagnostics
    assert found.items[0].source == "keyword" and found.items[0].symbol == "target"


def test_legacy_without_chunks_is_bound_whole_file_with_explicit_symbol_limitation(tmp_path):
    source = "# prefix\n" * 90 + "def target():\n    return 'late-source'\n"
    context = fixture_context(tmp_path, source)
    legacy = RetrievalContext.create(tmp_path, context.index, context.graph, binding=context.binding)
    result = retrieve(RetrievalRequest(Query("target")), legacy)
    item = result.items[0]
    assert item.snapshot_id == context.binding.snapshot_id
    assert item.chunk_ids == () and item.symbol is None
    assert item.coverage == "complete_file" and "symbol_range_unavailable" in item.diagnostics
    assert "chunks_missing" in result.diagnostics
    assert_exact(item, source)
    assert "late-source" in item.snippet


def test_source_empty_and_missing_are_not_reported_as_complete(tmp_path):
    context = fixture_context(tmp_path, "")
    result = retrieve_impact("logic.py", tmp_path, context.index, context.graph, context=context)
    assert result.status == "unavailable"
    assert result.items[0].start_line is None and result.items[0].coverage == "unavailable"
    (tmp_path / "logic.py").unlink()
    missing = RetrievalContext.create(tmp_path, context.index, CodeGraph(str(tmp_path)))
    result = retrieve_impact("logic.py", tmp_path, context.index, context.graph, context=missing)
    assert "source_unavailable" in result.diagnostics
    assert result.items[0].snippet == ""


def test_module_and_unicode_line_separators_use_chunk_line_numbers(tmp_path):
    source = "MAGIC_VALUE = 'one\u2028two'\n\ndef target():\n    return MAGIC_VALUE\n"
    context = fixture_context(tmp_path, source)
    result = retrieve(RetrievalRequest(Query("logic.py")), context)
    assert len(result.items) == 1 and result.items[0].end_line == 4
    assert_exact(result.items[0], source)


def test_chinese_adjacent_symbol_is_exact_and_limit_is_reported(tmp_path):
    source = "def repeated():\n    return 1\n" * 8
    context = fixture_context(tmp_path, source)
    result = retrieve(RetrievalRequest(Query("请解释repeated函数")), context)
    assert len(result.items) == 6 and all(item.source == "exact_symbol" for item in result.items)
    assert len({item.symbol_key for item in result.items}) == 6
    assert "candidate_limit_reached" in result.diagnostics
    assert all(item.coverage == "complete_symbol" for item in result.items)


def test_unindexed_target_and_foreign_workspace_cannot_supply_evidence(tmp_path):
    context = fixture_context(tmp_path, "x = 1\n")
    result = retrieve(RetrievalRequest(Query("explain", "explain", "../private.py")), context)
    assert result.items == [] and result.status == "empty"
    assert "target_not_indexed" in result.diagnostics
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    tool = retrieve_context(ToolContext(foreign, retrieval_context=context), "logic.py")
    assert not tool.ok and tool.error == "trusted_workspace_mismatch"


def test_graph_tool_uses_same_trusted_graph_and_rejects_path_replacement(tmp_path, monkeypatch):
    from pycode.tools import query_code_graph
    from dataclasses import asdict
    context = fixture_context(tmp_path, "def main():\n    return 1\n", filename="main.py")
    def forbidden(*args, **kwargs):
        pytest.fail("bound Graph tool must not load a model-selected Graph")
    monkeypatch.setattr("pycode.tools.query_graph.load_graph", forbidden)
    tool_context = ToolContext(tmp_path, retrieval_context=context)
    result = query_code_graph(tool_context, "entry")
    assert result.ok and result.data["nodes"][0]["path"] == "main.py"
    assert result.data["binding"] == asdict(context.binding)
    assert result.data["relation_status"] == "stored_static_resolution_unverified"
    denied = query_code_graph(tool_context, "entry", graph_path="alternate.json")
    assert not denied.ok and denied.error == "trusted_artifacts_cannot_be_overridden"
