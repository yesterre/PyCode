"""Offline, Commit-pinned retrieval baseline; never creates a model client.

Run from the repository: python -m scripts.benchmarks.a00_baseline
The corpus is read from Git objects, not from the mutable working directory.
"""
from __future__ import annotations

import argparse
import ast
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import platform
import statistics
import subprocess
from tempfile import TemporaryDirectory
from time import perf_counter

from pycode.engine import PyCodeEngine
from pycode.graph_builder import build_code_graph
from pycode.retriever import retrieve_for_question, retrieve_impact
from pycode.storage import save_graph, save_index


ROOT = Path(__file__).resolve().parents[2]
STAGE = ROOT / "docs_v3.0" / "v3_agent" / "A00"
CORPUS_PREFIXES = ("pycode/", "backend/")


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def git(repo: Path, *arguments: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *arguments], check=True, capture_output=True,
    ).stdout


def read_corpus(repo: Path, commit: str) -> dict[str, bytes]:
    # Require a full immutable object ID, never a moving branch or arbitrary revspec.
    if len(commit) != 40 or any(char not in "0123456789abcdef" for char in commit):
        raise ValueError("Dataset requires a full lowercase Git Commit SHA.")
    resolved = git(repo, "rev-parse", "--verify", f"{commit}^{{commit}}").decode().strip()
    if resolved != commit:
        raise ValueError("Dataset Commit mismatch.")
    paths = git(repo, "ls-tree", "-r", "--name-only", "-z", commit).decode().split("\0")
    return {
        path: git(repo, "show", f"{commit}:{path}")
        for path in sorted(paths)
        if path.startswith(CORPUS_PREFIXES) and path.endswith(".py")
    }


def symbol_span(source: bytes, symbol: str) -> tuple[int, int]:
    body = ast.parse(source.decode("utf-8-sig")).body
    node = None
    for name in symbol.split("."):
        node = next((item for item in body if isinstance(
            item, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef),
        ) and item.name == name), None)
        if node is None:
            raise ValueError(f"Ground Truth symbol is missing: {symbol}")
        body = node.body
    return node.lineno, node.end_lineno


def validate_dataset(dataset: dict, corpus: dict[str, bytes]) -> None:
    if dataset["schema_version"] != 1 or not dataset["cases"]:
        raise ValueError("Unsupported or empty question set.")
    if dataset["corpus_prefixes"] != list(CORPUS_PREFIXES):
        raise ValueError("Corpus scope mismatch.")
    ids = [case["id"] for case in dataset["cases"]]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate question IDs.")
    for case in dataset["cases"]:
        if case["truth_status"] not in {"source_verified", "unknown"}:
            raise ValueError("Unknown Ground Truth status.")
        if bool(case["evidence"]) != (case["truth_status"] == "source_verified"):
            raise ValueError("Ground Truth evidence/status mismatch.")
        if case["mode"] not in {"ask", "impact", "agent"}:
            raise ValueError("Unsupported baseline mode.")
        if case["mode"] == "impact" and case["target"] not in corpus:
            raise ValueError("Impact target is outside the pinned corpus.")
        for evidence in case["evidence"]:
            source = corpus[evidence["path"]]
            if sha256(source) != evidence["source_sha256"]:
                raise ValueError("Ground Truth source hash mismatch.")
            if symbol_span(source, evidence["symbol"]) != (
                evidence["start_line"], evidence["end_line"],
            ):
                raise ValueError("Ground Truth symbol span mismatch.")


def materialize(corpus: dict[str, bytes], destination: Path) -> None:
    for relative, source in corpus.items():
        path = PurePosixPath(relative)
        if path.is_absolute() or ".." in path.parts or ":" in relative or "\\" in relative:
            raise ValueError("Unsafe corpus path.")
        target = destination / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source)


def score(items: list, evidence: list[dict], k: int) -> dict:
    if k < 1:
        raise ValueError("K must be positive.")
    if not evidence:
        return {"recall": None, "reciprocal_rank": None, "snippet_line_coverage": None}
    relevant = {entry["path"] for entry in evidence}
    selected = list(dict.fromkeys(item.path for item in items if item.path))[:k]
    hits = relevant.intersection(selected)
    rank = next((rank for rank, path in enumerate(selected, 1) if path in relevant), None)
    expected_lines = {
        (entry["path"], line)
        for entry in evidence
        for line in range(entry["start_line"], entry["end_line"] + 1)
    }
    displayed = set()
    for item in items:
        if item.path not in selected:
            continue
        for line in item.snippet.splitlines():
            prefix, separator, _ = line.partition(": ")
            if separator and prefix.isdecimal():
                displayed.add((item.path, int(prefix)))
    return {
        "recall": len(hits) / len(relevant),
        "reciprocal_rank": 1 / rank if rank else 0.0,
        "snippet_line_coverage": len(expected_lines & displayed) / len(expected_lines),
    }


class CaptureStub:
    """Checks the transport seam only; this text is never scored as an answer."""
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return "A00 offline transport stub; answer quality is not evaluated."


def run_baseline(repo: Path, dataset_path: Path, *, repeats: int = 5) -> dict:
    if repeats < 1:
        raise ValueError("Repeats must be positive.")
    dataset_bytes = dataset_path.read_bytes()
    dataset = json.loads(dataset_bytes)
    corpus = read_corpus(repo, dataset["commit"])
    validate_dataset(dataset, corpus)
    corpus_hashes = {path: sha256(source) for path, source in corpus.items()}
    engine = PyCodeEngine()
    rows = []
    with TemporaryDirectory(prefix="pycode-a00-") as temporary:
        root = Path(temporary)
        materialize(corpus, root)
        started = perf_counter()
        index = engine.build_index(root)
        graph = build_code_graph(index)
        build_ms = (perf_counter() - started) * 1000
        save_index(index, root / ".pclens" / "index.json")
        save_graph(graph, root / ".pclens" / "code_graph.json")
        for case in dataset["cases"]:
            samples = []
            for _ in range(repeats):
                started = perf_counter()
                retrieval = (
                    retrieve_impact(case["target"], root, index, graph, case["question"])
                    if case["mode"] == "impact" else
                    retrieve_for_question(case["question"], root, index, graph)
                )
                samples.append((perf_counter() - started) * 1000)
            stub = CaptureStub()
            if case["mode"] == "agent":
                result = engine.run_agent(
                    root, case["question"], llm_client=stub, use_llm_planner=False,
                    enable_memory=False, enable_memory_extraction=False,
                )
                pipeline = {"kind": "rule_agent_stub", "ok": result.ok,
                            "stop_reason": str(result.stop_reason), "turns": len(result.turns),
                            "tools": [item.tool for item in result.tool_results]}
            else:
                result = (engine.impact(root, case["target"], llm_client=stub)
                          if case["mode"] == "impact" else
                          engine.ask(root, case["question"], llm_client=stub))
                pipeline = {"kind": "answer_stub", "ok": result.answer ==
                            "A00 offline transport stub; answer quality is not evaluated."}
            rows.append({
                "id": case["id"], "mode": case["mode"], "intent": retrieval.intent,
                "retrieved_paths": [item.path for item in retrieval.items],
                "metrics": {str(k): score(retrieval.items, case["evidence"], k) for k in (1, 3, 6)},
                "retrieval_ms": samples,
                "prompt_chars": [len(prompt) for prompt in stub.prompts],
                "prompt_sha256": [sha256(prompt.encode()) for prompt in stub.prompts],
                "pipeline": pipeline,
            })
    measured = [row for row in rows if row["metrics"]["6"]["recall"] is not None]
    summary = {
        str(k): {metric: statistics.mean(row["metrics"][str(k)][metric] for row in measured)
                 for metric in ("recall", "reciprocal_rank", "snippet_line_coverage")}
        for k in (1, 3, 6)
    }
    times = sorted(value for row in rows for value in row["retrieval_ms"])
    # Fingerprint the evaluated implementation separately from the pinned corpus.
    implementation = {
        path.relative_to(ROOT).as_posix(): sha256(path.read_bytes())
        for path in sorted((ROOT / "pycode").rglob("*.py"))
    }
    return {
        "schema_version": 1, "created_at": datetime.now(timezone.utc).isoformat(),
        "commit": dataset["commit"], "snapshot_binding": "git:" + dataset["commit"],
        "snapshot_kind": "offline Git corpus; no database ProjectSnapshot created",
        "dataset_sha256": sha256(dataset_bytes),
        "corpus_sha256": sha256(json.dumps(corpus_hashes, sort_keys=True).encode()),
        "corpus_files": corpus_hashes, "implementation_files": implementation,
        "runner_sha256": sha256(Path(__file__).read_bytes()),
        "environment": {"python": platform.python_version(), "os": platform.platform(),
                        "provider": "CaptureStub", "model": None, "network_calls": 0,
                        "embedding": None, "repeats": repeats, "warmup": 0},
        "index": {"files": len(index.files), "nodes": len(graph.nodes),
                  "edges": len(graph.edges), "build_ms": build_ms},
        "summary": summary, "scored_cases": len(measured), "total_cases": len(rows),
        "retrieval_p50_ms": statistics.median(times),
        "retrieval_p95_ms": times[math.ceil(len(times) * .95) - 1],
        "answer_correctness": None, "groundedness": None,
        "prompt_tokens": None, "completion_tokens": None, "model_cost": None,
        "paid_requests": 0, "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=STAGE / "questions.json")
    parser.add_argument("--output", type=Path, default=STAGE / "baseline.json")
    parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args()
    result = run_baseline(ROOT, args.dataset, repeats=args.repeats)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"scored_cases": result["scored_cases"], "summary": result["summary"]}))


if __name__ == "__main__":
    main()
