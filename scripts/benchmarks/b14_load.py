"""Opt-in loopback HTTP experiment; invoked from a repository checkout.

python -m scripts.benchmarks.b14_load --requests 100 --concurrency 1,4,8 --rounds 3 --compare
The pytest entry reuses the existing guarded database and offline source fixtures.
"""
from collections import Counter, defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import argparse
import ctypes
import json
import math
import os
from pathlib import Path
import platform
import socket
import subprocess
from threading import Condition, Event, Lock, Thread
from time import monotonic, perf_counter, sleep
from uuid import UUID, uuid4
from concurrent.futures import ThreadPoolExecutor

import httpx
from sqlalchemy import event, text
import uvicorn


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "docs_v3.0" / "v3_backend"
OPERATIONS = ("login", *(["projects"] * 8), *(["history"] * 8), "submit", "task", "task")
operation_context = ContextVar("b14_operation", default="worker")


def percentile(values, fraction):
    """Nearest-rank percentile; retain raw values instead of asserting timings."""
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


def summarize(samples, elapsed):
    groups = {}
    for name in ("all", *dict.fromkeys(OPERATIONS)):
        rows = samples if name == "all" else [row for row in samples if row["operation"] == name]
        times = [row["ms"] for row in rows]
        groups[name] = {
            "count": len(rows), "p50_ms": percentile(times, .5), "p95_ms": percentile(times, .95),
            "statuses": dict(Counter(str(row["status"]) for row in rows)),
            "error_rate": sum(row["status"] >= 400 or row["status"] == 0 for row in rows) / len(rows) if rows else 0,
        }
    return {"http_wall_seconds": elapsed, "requests_per_second": len(samples) / elapsed, "operations": groups}


def machine():
    result = {"os": platform.platform(), "cpu": platform.processor(), "logical_cpus": os.cpu_count(),
              "python": platform.python_version(), "deployment": "native loopback API; no containers started",
              "api_process_limits": "Native OS defaults; no affinity or memory cap set by harness"}
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in ("total", "available", "page_total", "page_available", "virtual_total", "virtual_available", "extended")]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            result["physical_memory_bytes"] = status.total
    elif hasattr(os, "sysconf"):
        result["physical_memory_bytes"] = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    return result


def container_resources(host, port):
    """Only read limits of containers publishing the actual Redis port; no env dump."""
    if host not in {"localhost", "127.0.0.1", "::1"}:
        return {"unverified": "Remote Redis; local container limits are not attributed"}
    def command(arguments):
        completed = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=8)
        if completed.returncode:
            raise RuntimeError("Docker metadata unavailable")
        return completed.stdout.strip()
    try:
        vm = json.loads(command(["info", "--format", '{"cpus":{{.NCPU}},"memory_bytes":{{.MemTotal}}}']))
        ids = command(["ps", "--filter", f"publish={port}", "--format", "{{.ID}}"]).split()
        template = ('{"name":{{json .Name}},"nano_cpus":{{.HostConfig.NanoCpus}},'
                    '"memory_bytes":{{.HostConfig.Memory}},"cpu_quota":{{.HostConfig.CpuQuota}},'
                    '"cpu_period":{{.HostConfig.CpuPeriod}},"ports":{{json .NetworkSettings.Ports}}}')
        containers = [json.loads(line) for line in command(["inspect", *ids, "--format", template]).splitlines()] if ids else []
        return {"engine": vm, "redis_port_containers": containers, "zero_limit_meaning": "No explicit per-container cap"}
    except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError):
        return {"unverified": "Docker metadata unavailable; no container limits assumed"}


def comparisons(phases):
    result = {}
    for concurrency in sorted({phase["concurrency"] for phase in phases}):
        variants = {}
        for variant in ("before", "after"):
            rows = [phase for phase in phases if phase["concurrency"] == concurrency and phase["variant"] == variant]
            if not rows:
                continue
            rates = [phase["requests_per_second"] for phase in rows]
            samples = [sample for phase in rows for sample in phase["samples"]]
            variants[variant] = {"rps_samples": rates, "median_rps": percentile(rates, .5),
                                 "pooled": summarize(samples, sum(phase["http_wall_seconds"] for phase in rows)),
                                 "project_selects_per_request": [sum(call["count"] for call in phase["telemetry"]["calls"]
                                     if call["category"] == "sql" and call["operation"] == "projects" and call["name"] == "SELECT") /
                                     phase["operations"]["projects"]["count"] for phase in rows],
                                 "queue_wait_p95_ms": percentile([wait for phase in rows for wait in phase["queue_wait_ms"]], .95)}
        if set(variants) == {"before", "after"}:
            before, after = variants["before"], variants["after"]
            result[str(concurrency)] = {**variants,
                "throughput_change_pct": (after["median_rps"] / before["median_rps"] - 1) * 100,
                "project_p95_change_pct": (after["pooled"]["operations"]["projects"]["p95_ms"] /
                                           before["pooled"]["operations"]["projects"]["p95_ms"] - 1) * 100,
                "error_free_comparison": all(value["pooled"]["operations"]["all"]["error_rate"] == 0 for value in variants.values())}
    return result


def frozen_list_owned_page(self, owner_id, limit, offset, *, archived=False):
    """Exact pre-B14 service body from e243419; authorization/pagination stay on."""
    with self.uow.transaction():
        page = self.uow.projects.list_owned_page(owner_id, limit + 1, offset, archived=archived)
        return [self._with_summary(project) for project in page[:limit]], len(page) > limit


class Telemetry:
    def __init__(self):
        self.lock = Lock()
        self.active = False
        self.values = defaultdict(list)
        self.checked_out = self.max_checked_out = 0

    def add(self, category, name, elapsed):
        with self.lock:
            if self.active:
                self.values[category, operation_context.get(), name].append(elapsed * 1000)

    def begin(self):
        with self.lock:
            self.values.clear()
            self.max_checked_out = self.checked_out
            self.active = True

    def end(self):
        with self.lock:
            self.active = False
            return {"calls": [{"category": category, "operation": operation, "name": name,
                               "count": len(values), "sum_ms": sum(values),
                               "p50_ms": percentile(values, .5), "p95_ms": percentile(values, .95)}
                              for (category, operation, name), values in sorted(self.values.items())],
                    "max_pool_checked_out": self.max_checked_out}

    def wrap(self, category, name, function):
        def observed(*args, **kwargs):
            started = perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                self.add(category, name, perf_counter() - started)
        return observed

    def install(self, engine, monkeypatch):
        import backend.app.config as config
        import backend.app.db.session as database
        from redis import Redis

        monkeypatch.setattr(config, "load_environment", self.wrap("environment", "config", config.load_environment))
        monkeypatch.setattr(database, "load_environment", self.wrap("environment", "session", database.load_environment))
        original = Redis.execute_command

        def redis_command(client, *args, **kwargs):
            name = str(args[0]).upper() if args else "OTHER"
            return self.wrap("redis", name, original)(client, *args, **kwargs)

        monkeypatch.setattr(Redis, "execute_command", redis_command)

        def before(connection, cursor, statement, parameters, context, many):
            context.b14_started = perf_counter()

        def after(connection, cursor, statement, parameters, context, many):
            self.add("sql", statement.lstrip().split(None, 1)[0].upper(), perf_counter() - context.b14_started)

        def checkout(*args):
            with self.lock:
                self.checked_out += 1
                self.max_checked_out = max(self.max_checked_out, self.checked_out)

        def checkin(*args):
            with self.lock:
                self.checked_out -= 1

        listeners = [(engine, "before_cursor_execute", before), (engine, "after_cursor_execute", after),
                     (engine.pool, "checkout", checkout), (engine.pool, "checkin", checkin)]
        for target, name, function in listeners:
            event.listen(target, name, function)
        return lambda: [event.remove(target, name, function) for target, name, function in listeners]


class OperationMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        name = dict(scope.get("headers", [])).get(b"x-b14-operation", b"other").decode("ascii", "replace")
        token = operation_context.set(name if name in OPERATIONS else "other")
        try:
            await self.app(scope, receive, send)
        finally:
            operation_context.reset(token)


@contextmanager
def loopback_server(app):
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        bound.listen(128)
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, loop="asyncio", log_level="info"))
        thread = Thread(target=server.run, kwargs={"sockets": [bound]}, daemon=True)
        thread.start()
        try:
            deadline = monotonic() + 30
            while not server.started:
                if not thread.is_alive() or monotonic() > deadline:
                    raise RuntimeError("Isolated HTTP server did not start")
                sleep(.01)
            yield f"http://127.0.0.1:{bound.getsockname()[1]}"
        finally:
            server.should_exit = True
            thread.join(30)
            if thread.is_alive():
                raise RuntimeError("Isolated HTTP server did not stop")


class IndexConsumer:
    """Real Kombu/Redis consumer; production execution/lease services, one thread."""
    def __init__(self, celery, queue, app):
        self.celery, self.queue, self.app = celery, queue, app
        self.stop = Event()
        self.condition = Condition()
        self.completed, self.errors = set(), []
        self.thread = Thread(target=self.run, daemon=True)

    def run(self):
        from queue import Empty
        from backend.app.services.background_tasks import REPOSITORY_INDEX_CELERY_TASK_NAME
        from backend.app.tasks.indexing import run_repository_index_task

        try:
            with self.celery.connection_for_read() as connection, connection.SimpleQueue(self.queue) as queue:
                while not self.stop.is_set():
                    try:
                        message = queue.get(block=True, timeout=.5)
                    except Empty:
                        continue
                    try:
                        assert message.headers["task"] == REPOSITORY_INDEX_CELERY_TASK_NAME
                        task_id = UUID(message.payload[0][0])
                        run_repository_index_task.push_request(id=message.headers.get("id"), hostname="b14-consumer")
                        try:
                            run_repository_index_task.run(str(task_id))
                        finally:
                            run_repository_index_task.pop_request()
                        with self.condition:
                            self.completed.add(task_id)
                            self.condition.notify_all()
                    finally:
                        message.ack()
        except BaseException as exc:
            with self.condition:
                self.errors.append(type(exc).__name__)
                self.condition.notify_all()

    def wait(self, identifiers):
        with self.condition:
            done = self.condition.wait_for(lambda: self.errors or set(identifiers) <= self.completed, timeout=90)
            if not done or self.errors:
                raise RuntimeError("Benchmark consumer failed or exceeded drain deadline: " + ",".join(self.errors))

    def close(self):
        self.stop.set()
        if self.thread.ident is not None:
            self.thread.join(10)
            if self.thread.is_alive():
                raise RuntimeError("Benchmark consumer did not stop")


@dataclass
class Actor:
    account: str
    headers: dict
    history_project: UUID
    index_project: UUID
    owned_projects: set
    history_ids: list
    task_id: str = ""


class Workload:
    def __init__(self, client, actors, password, consumer, telemetry, engine, metrics_token, run_id):
        self.client, self.actors, self.password = client, actors, password
        self.consumer, self.telemetry, self.engine = consumer, telemetry, engine
        self.metrics_token, self.run_id = metrics_token, run_id

    def request(self, name, actor, key):
        headers = {**actor.headers, "X-B14-Operation": name}
        if name == "login":
            method, path, body = "POST", "/api/v1/auth/login", {"account_name": actor.account, "password": self.password}
        elif name == "projects":
            method, path, body = "GET", "/api/v1/projects?limit=50", None
        elif name == "history":
            method, path, body = "GET", f"/api/v1/projects/{actor.history_project}/analysis-history?limit=20&offset=1000", None
        elif name == "submit":
            method, path, body = "POST", f"/api/v1/projects/{actor.index_project}/index", None
            headers["Idempotency-Key"] = key
        else:
            method, path, body = "GET", f"/api/v1/tasks/{actor.task_id}", None
        started = perf_counter()
        try:
            response = self.client.request(method, path, headers=headers, **({"json": body} if body is not None else {}))
        except httpx.HTTPError as exc:
            return {"operation": name, "status": 0, "ms": (perf_counter() - started) * 1000,
                    "exception_type": type(exc).__name__}, None
        elapsed = (perf_counter() - started) * 1000
        data = response.json()
        if response.status_code in {200, 202}:
            if name == "projects":
                assert len(data["projects"]) == 50 and data["has_more"]
                assert all(UUID(project["id"]) in actor.owned_projects for project in data["projects"])
                assert any(project["index_summary"] is not None for project in data["projects"])
            elif name == "history":
                assert data["has_more"] and [item["id"] for item in data["items"]] == actor.history_ids
            elif name == "task":
                assert data["project_id"] == str(actor.index_project)
                assert data["id"] == path.rsplit("/", 1)[-1]
            elif name == "login":
                assert data["token_type"] == "bearer" and data["access_token"]
            else:
                actor.task_id = data["task_id"]
        row = {"operation": name, "status": response.status_code, "ms": elapsed}
        if response.status_code >= 400:
            row["error_code"] = data.get("detail", {}).get("code", "http_error")
        return row, UUID(data["task_id"]) if name == "submit" and response.status_code == 202 else None

    def metrics(self):
        response = self.client.get("/metrics", headers={"Authorization": "Bearer " + self.metrics_token})
        assert response.status_code == 200
        return response.text

    def phase(self, variant, concurrency, requests, number):
        before = self.metrics()
        self.telemetry.begin()
        started = perf_counter()
        def perform(index):
            name = OPERATIONS[index % len(OPERATIONS)]
            actor = self.actors[(number * (requests // 20) + index // 20) % len(self.actors)]
            return self.request(name, actor, f"{self.run_id}:{number}:{index}")
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            results = list(executor.map(perform, range(requests)))
        elapsed = perf_counter() - started
        accepted = [identifier for _, identifier in results if identifier is not None]
        self.consumer.wait(accepted)
        telemetry = self.telemetry.end()
        after = self.metrics()
        samples = [row for row, _ in results]
        def http_counter(raw):
            return sum(float(line.rsplit(" ", 1)[1]) for line in raw.splitlines()
                       if line.startswith("pycode_http_requests_total{"))
        observed_requests = http_counter(after) - http_counter(before) - 1  # preceding scrape observes itself after sending
        assert observed_requests == requests
        with self.engine.connect() as connection:
            waits = connection.execute(text("""
                SELECT extract(epoch FROM started_at-created_at), attempt_count, status
                FROM background_tasks WHERE id=ANY(:ids)
            """), {"ids": accepted}).all() if accepted else []
            assert all(status == "completed" and attempts == 1 for _, attempts, status in waits)
        return {"variant": variant, "concurrency": concurrency, "samples": samples, "observed_http_requests": observed_requests,
                **summarize(samples, elapsed), "telemetry": telemetry,
                "accepted_tasks": len(accepted), "drain_wall_seconds": perf_counter() - started,
                "queue_wait_ms": [float(wait) * 1000 for wait, _, _ in waits],
                "metrics_before": before, "metrics_after": after}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--concurrency", default="1,4,8")
    parser.add_argument("--compare", action="store_true")
    args = parser.parse_args()
    try:
        levels = [int(value) for value in args.concurrency.split(",")]
    except ValueError:
        parser.error("concurrency must be comma-separated integers")
    if not 20 <= args.requests <= 1000 or args.requests % 20 or not 1 <= args.rounds <= 5 or not levels or any(not 1 <= value <= 32 for value in levels):
        parser.error("requests: multiple of 20 in 20..1000; rounds: 1..5; concurrency: 1..32")
    from backend.app.config import load_environment
    load_environment()
    if not os.environ.get("TEST_DATABASE_URL") or not (os.environ.get("TEST_REDIS_URL") or os.environ.get("REDIS_URL")):
        parser.error("A guarded TEST_DATABASE_URL and test Redis are required")
    os.environ["PYCODE_RUN_B14_LOAD"] = "1"
    os.environ["PYCODE_B14_OPTIONS"] = json.dumps(vars(args))
    import pytest
    raise SystemExit(pytest.main([str(ROOT / "tests/backend/test_b14_load.py::test_mixed_http_load"),
        "--basetemp=" + str(ROOT / (".pytest_tmp_b14_load_" + uuid4().hex[:10])),
        "-o", "cache_dir=" + str(OUTPUT / ".pytest_cache_b14"),
        "--junitxml=" + str(OUTPUT / ("B14_load_" + uuid4().hex[:10] + ".xml")), "--tb=short", "-ra"]))


if __name__ == "__main__":
    main()
