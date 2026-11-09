"""B12-only process diagnostics; never changes Worker deadlines or business state."""
import json
import os
from pathlib import Path
import re
import subprocess
from threading import Lock, Thread
import time
from urllib.parse import unquote, urlsplit


class StageRecorder:
    def __init__(self, path):
        self.path = Path(path)
        self.path.touch(exist_ok=False)
        self._guard = Lock()
        self._sequence = 0

    def record(self, stage, **details):
        # Callers supply fixed stage names and selected non-secret metadata only.
        with self._guard:
            event = dict(sequence=self._sequence, stage=stage, pid=os.getpid(),
                         monotonic_ns=time.monotonic_ns(), wall_time_ns=time.time_ns(), **details)
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._sequence += 1


def output_secrets(environment):
    result = []
    for key, value in environment.items():
        if not value:
            continue
        if key.endswith(("_URL", "_PASSWORD", "_SECRET", "_API_KEY", "_TOKEN")):
            result.append(value)
        if key.endswith("_URL"):
            try:
                password = urlsplit(value).password
                if password:
                    result.extend((password, unquote(password)))
            except ValueError:
                pass
    return sorted(set(result), key=len, reverse=True)


def redact_output(value, secrets):
    for secret in secrets:
        value = value.replace(secret, "[redacted-credential]")
    value = re.sub(r"(?i)\b(?:postgres(?:ql)?(?:\+psycopg)?|rediss?)://[^\s\"'<>]+",
                   "[redacted-connection-url]", value)
    value = re.sub(r"(?i)Bearer\s+[A-Za-z0-9_.~+/=-]+", "Bearer [redacted]", value)
    value = re.sub(r"(?i)(['\"]?(?:password|api_key|secret|execution_token|authorization)['\"]?\s*[:=]\s*)"
                   r"(?:UUID\([^)]*\)|'[^']*'|\"[^\"]*\"|[^\s,}\]>]+)",
                   r"\1'[redacted]'", value)
    # Raw SQL exception parameters can contain an execution token without a key.
    # Task identity remains available in the selected parent metadata, not raw logs.
    return re.sub(r"(?i)\b[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\b",
                  "[redacted-uuid]", value)


def read_events(path):
    if not path.exists():
        return [], 0
    events, incomplete = [], 0
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get("stage"), str):
                raise ValueError("Invalid diagnostic shape")
            events.append(event)
        except ValueError:
            incomplete += 1
    return events, incomplete


class DiagnosedWorker:
    """Popen-compatible methods used by B12, with continuously drained pipes."""
    def __init__(self, command, *, directory, env, metadata=None, **kwargs):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.recorder = StageRecorder(self.directory / "parent-events.jsonl")
        self._secrets = output_secrets(env)
        self._buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self._reader_errors = {}
        self._captured = False
        self.polls = self.missing = self.decode_errors = 0
        self.last_polled_returncode = None
        self.recorder.record("popen_started", **(metadata or {}))
        try:
            self.process = subprocess.Popen(command, env=env, stdout=subprocess.PIPE,
                                            stderr=subprocess.PIPE, **kwargs)
        except BaseException as exc:
            self.recorder.record("popen_failed", exception_type=type(exc).__name__)
            raise
        self.recorder.record("popen_returned", child_pid=self.process.pid)
        self._readers = []
        for name in self._buffers:
            reader = Thread(target=self._drain, args=(name,), daemon=True)
            reader.start()
            self._readers.append(reader)

    def _drain(self, name):
        pipe = getattr(self.process, name)
        try:
            while chunk := pipe.read1(65536):
                self._buffers[name].extend(chunk)
        except Exception as exc:
            self._reader_errors[name] = type(exc).__name__
        finally:
            pipe.close()

    @property
    def pid(self):
        return self.process.pid

    @property
    def returncode(self):
        return self.process.returncode

    def poll(self):
        self.polls += 1
        code = self.process.poll()
        if self.polls == 1 or code != self.last_polled_returncode:
            self.recorder.record("parent_polled_child", alive=code is None, returncode=code)
        self.last_polled_returncode = code
        return code

    def read_checkpoint(self, path):
        if not path.exists():
            self.missing += 1
            if self.missing == 1:
                self.recorder.record("checkpoint_initially_missing")
            return False
        try:
            result = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            self.decode_errors += 1
            if self.decode_errors == 1:
                self.recorder.record("checkpoint_json_decode_failed")
            return False
        except OSError as exc:
            self.recorder.record("checkpoint_read_failed", exception_type=type(exc).__name__)
            raise
        self.recorder.record("checkpoint_json_decoded", is_mapping=isinstance(result, dict))
        return result

    def kill(self):
        self.recorder.record("parent_kill_requested", alive=self.process.poll() is None)
        self.process.kill()

    def record(self, stage, **details):
        self.recorder.record(stage, **details)
        if self._captured:
            self._save_summary()

    def communicate(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        try:
            self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.recorder.record("parent_wait_timed_out", alive=self.process.poll() is None)
            self._save_summary()
            raise
        for reader in self._readers:
            reader.join(timeout=None if deadline is None else max(0, deadline - time.monotonic()))
        if any(reader.is_alive() for reader in self._readers):
            self.recorder.record("pipe_reader_did_not_finish")
            self._save_summary()
            raise subprocess.TimeoutExpired(self.process.args, timeout)
        if not self._captured:
            for name, buffer in self._buffers.items():
                value = redact_output(buffer.decode("utf-8", errors="replace"), self._secrets)
                with (self.directory / (name + ".log")).open("x", encoding="utf-8") as stream:
                    stream.write(value)
            self._captured = True
            self.recorder.record("child_exited_and_streams_captured", returncode=self.returncode)
        self._save_summary()
        return tuple((self.directory / (name + ".log")).read_text(encoding="utf-8")
                     for name in ("stdout", "stderr"))

    def _save_summary(self):
        child, incomplete = read_events(self.directory / "child-events.jsonl")
        parent, _ = read_events(self.recorder.path)
        completed = {"python_started", "redis_imports_completed", "backend_imports_completed",
                     "fixture_imports_completed", "dependency_imports_completed", "database_url_validated",
                     "session_factory_ready", "database_connected", "services_ready", "claim_returned",
                     "claim_transaction_committed", "checkpoint_write_completed"}
        last_completed = next((e["stage"] for e in reversed(child) if e["stage"] in completed), None)
        data = dict(child_pid=self.pid, returncode=self.returncode,
                    parent_last_stage=parent[-1]["stage"] if parent else None,
                    child_last_stage=child[-1]["stage"] if child else None,
                    child_last_completed_phase=last_completed, child_event_count=len(child),
                    incomplete_child_event_lines=incomplete, poll_count=self.polls,
                    missing_checkpoint_polls=self.missing, json_decode_failure_polls=self.decode_errors,
                    stdout_bytes=len(self._buffers["stdout"]), stderr_bytes=len(self._buffers["stderr"]),
                    reader_errors=self._reader_errors, streams_captured=self._captured)
        (self.directory / "summary.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
