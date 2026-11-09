"""Small offline contracts for B12 diagnostics, not another Worker test matrix."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from b12_probe_diagnostics import DiagnosedWorker, StageRecorder


def spawn(tmp_path, program, **environment):
    interpreter = getattr(sys, "_base_executable", sys.executable) if os.name == "nt" else sys.executable
    return DiagnosedWorker([interpreter, "-u", "-c", program],
        directory=tmp_path / "diagnostics", env={**os.environ, **environment},
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def stop(child):
    if child.poll() is None:
        child.kill()
    return child.communicate(timeout=5)


def test_large_unterminated_output_is_drained_before_checkpoint_and_redacted(tmp_path):
    signal = tmp_path / "checkpoint.json"
    secret = "unicode-boundary-密码-test-only"
    url = "postgresql+psycopg://fixture:fixture-password@localhost/pycode_test"
    program = (
        "import os,json,pathlib,time\n"
        f"payload=('x'*262141+{secret!r}+' '+{url!r}+' 诊断尾部').encode('utf-8')\n"
        "os.write(2,payload)\n"
        "os.write(1,b'controlled stdout')\n"
        f"pathlib.Path({str(signal)!r}).write_text(json.dumps({{'pid':os.getpid()}}),encoding='utf-8')\n"
        "time.sleep(20)\n"
    )
    child = spawn(tmp_path, program, TEST_DATABASE_URL=url, FIXTURE_SECRET=secret)
    try:
        deadline = time.monotonic() + 5
        value = False
        while time.monotonic() < deadline:
            value = child.read_checkpoint(signal)
            if value:
                break
            time.sleep(.01)
        assert value == {"pid": child.pid}
    finally:
        stdout, stderr = stop(child)
    assert stdout == "controlled stdout"
    assert stderr.endswith("诊断尾部") and len(stderr) > 262000
    assert secret not in stderr and url not in stderr and "fixture-password" not in stderr
    assert child.communicate(timeout=1) == (stdout, stderr)  # Same idempotent usage as B12 finally.
    summary = json.loads((child.directory / "summary.json").read_text(encoding="utf-8"))
    assert summary["streams_captured"] and summary["stderr_bytes"] > 262144
    assert summary["reader_errors"] == {} and child.returncode != 0


def test_early_exit_retains_streams_exit_code_and_partial_child_stage(tmp_path):
    stage_path = tmp_path / "diagnostics" / "child-events.jsonl"
    events = (b'{"stage":"python_started"}\n'
              b'{"stage":"backend_imports_started"}\n' + b'{"stage":"\xe4\xb8')
    program = (
        "import pathlib,sys\n"
        f"pathlib.Path({str(stage_path)!r}).write_bytes({events!r})\n"
        "sys.stderr.write('controlled import failure; password=fixture-password')\n"
        "sys.exit(7)\n"
    )
    child = spawn(tmp_path, program, FIXTURE_PASSWORD="fixture-password")
    stdout, stderr = child.communicate(timeout=5)
    assert child.returncode == 7 and stdout == ""
    assert "controlled import failure" in stderr and "fixture-password" not in stderr
    summary = json.loads((child.directory / "summary.json").read_text(encoding="utf-8"))
    assert summary["child_last_stage"] == "backend_imports_started"
    assert summary["child_last_completed_phase"] == "python_started"
    assert summary["incomplete_child_event_lines"] == 1 and summary["streams_captured"]


def test_checkpoint_missing_partial_json_and_io_failure_remain_distinct(tmp_path):
    child = spawn(tmp_path, "import time; time.sleep(20)")
    signal = tmp_path / "checkpoint.json"
    try:
        assert child.read_checkpoint(signal) is False
        signal.write_text("{", encoding="utf-8")
        assert child.read_checkpoint(signal) is False
        signal.write_text('{"stage":"claim"}', encoding="utf-8")
        assert child.read_checkpoint(signal) == {"stage": "claim"}
        with pytest.raises(OSError):
            child.read_checkpoint(tmp_path)  # Original non-JSON IO errors must propagate.
        with pytest.raises(subprocess.TimeoutExpired):
            child.communicate(timeout=.01)
        waiting = json.loads((child.directory / "summary.json").read_text(encoding="utf-8"))
        assert waiting["returncode"] is None and not waiting["streams_captured"]
    finally:
        stop(child)
    summary = json.loads((child.directory / "summary.json").read_text(encoding="utf-8"))
    assert summary["missing_checkpoint_polls"] == summary["json_decode_failure_polls"] == 1
    stages = [json.loads(line)["stage"] for line in (child.directory / "parent-events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert "checkpoint_read_failed" in stages and "parent_wait_timed_out" in stages


def test_existing_diagnostic_evidence_cannot_be_overwritten(tmp_path):
    path = tmp_path / "stages.jsonl"
    recorder = StageRecorder(path)
    recorder.record("python_started")
    before = path.read_bytes()
    with pytest.raises(FileExistsError):
        StageRecorder(path)
    assert path.read_bytes() == before
