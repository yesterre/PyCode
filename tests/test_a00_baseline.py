"""Protect the audit's measurements, provenance and offline model boundary."""
from copy import deepcopy
import json
from pathlib import Path
import socket

import pytest

import pycode.engine
from pycode.retriever import ContextItem
from scripts.benchmarks.a00_baseline import (
    ROOT, STAGE, materialize, read_corpus, run_baseline, score, sha256,
    symbol_span, validate_dataset,
)


def test_metrics_use_unique_files_and_actual_displayed_lines():
    items = [ContextItem("other", path="other.py", snippet="1: no"),
             ContextItem("hit", path="hit.py", snippet="2: a\n3: b"),
             ContextItem("duplicate", path="hit.py", snippet="3: b")]
    evidence = [{"path": "hit.py", "start_line": 2, "end_line": 5},
                {"path": "missing.py", "start_line": 1, "end_line": 2}]
    assert score(items, evidence, 1) == {
        "recall": 0, "reciprocal_rank": 0, "snippet_line_coverage": 0,
    }
    assert score(items, evidence, 6) == {
        "recall": .5, "reciprocal_rank": .5, "snippet_line_coverage": 2 / 6,
    }


def test_empty_retrieval_is_zero_but_unknown_truth_is_unscored():
    truth = [{"path": "a.py", "start_line": 1, "end_line": 1}]
    assert all(value == 0 for value in score([], truth, 6).values())
    assert all(value is None for value in score([], [], 6).values())
    with pytest.raises(ValueError, match="positive"):
        score([], truth, 0)


@pytest.mark.parametrize("path", ["../outside.py", "/absolute.py", "C:/outside.py", "a\\b.py"])
def test_materialization_rejects_escaping_paths(tmp_path, path):
    with pytest.raises(ValueError, match="Unsafe"):
        materialize({path: b""}, tmp_path)
    assert not list(tmp_path.iterdir())


def test_truth_rejects_source_and_span_drift():
    source = b"class Service:\n    async def run(self):\n        return 1\n"
    evidence = {"path": "pycode/service.py", "symbol": "Service.run",
                "start_line": 2, "end_line": 3, "source_sha256": sha256(source)}
    dataset = {"schema_version": 1, "corpus_prefixes": ["pycode/", "backend/"],
               "cases": [{"id": "Q", "mode": "ask", "truth_status": "source_verified",
                          "evidence": [evidence]}]}
    corpus = {"pycode/service.py": source}
    validate_dataset(dataset, corpus)
    with pytest.raises(ValueError, match="hash mismatch"):
        validate_dataset(dataset, {"pycode/service.py": source + b"# drift\n"})
    changed = deepcopy(dataset)
    changed["cases"][0]["evidence"][0]["end_line"] = 4
    with pytest.raises(ValueError, match="span mismatch"):
        validate_dataset(changed, corpus)
    with pytest.raises(ValueError, match="symbol is missing"):
        symbol_span(source, "Service.absent")
    changed = deepcopy(dataset)
    changed["cases"].append(changed["cases"][0])
    with pytest.raises(ValueError, match="Duplicate"):
        validate_dataset(changed, corpus)


@pytest.mark.parametrize("revision", ["HEAD", "v3.0", "0" * 39, "A" * 40, "HEAD:path"])
def test_corpus_rejects_mutable_or_invalid_revisions(revision):
    with pytest.raises(ValueError, match="full lowercase"):
        read_corpus(ROOT, revision)


def test_pinned_question_set_and_real_pipelines_remain_offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("A00 must not create a paid client or connect to a network")

    monkeypatch.setattr(pycode.engine, "OpenAIResponsesClient", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    dataset = json.loads((STAGE / "questions.json").read_text(encoding="utf-8"))
    report = run_baseline(ROOT, STAGE / "questions.json", repeats=1)
    assert report["commit"] == dataset["commit"]
    assert report["snapshot_binding"] == "git:" + dataset["commit"]
    assert report["scored_cases"] == sum(bool(case["evidence"]) for case in dataset["cases"])
    assert report["paid_requests"] == 0
    assert report["prompt_tokens"] is None and report["model_cost"] is None
    assert report["answer_correctness"] is None and report["groundedness"] is None
    assert report["corpus_files"] and report["implementation_files"]
    for row in report["rows"]:
        assert row["prompt_chars"]
        assert set(row["retrieved_paths"]) <= report["corpus_files"].keys()
        assert len(row["retrieval_ms"]) == 1
        assert "prompt" not in row
    assert any(row["pipeline"]["kind"] == "rule_agent_stub" for row in report["rows"])
