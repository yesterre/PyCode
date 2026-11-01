"""Build and verify a uniquely named local Compose stack without paid APIs.

Run from the checkout: python -m scripts.deploy.smoke
Only this run's project/volumes and generated credential file are cleaned up.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener
from uuid import UUID, uuid4


ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "docs_v3.0" / "v3_backend"
HTTP = build_opener(ProxyHandler({}))


def run(arguments, *, env, timeout=240):
    result = subprocess.run(arguments, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        output = result.stdout + "\n" + result.stderr
        for name in ("POSTGRES_PASSWORD", "PYCODE_JWT_SECRET", "PYCODE_METRICS_TOKEN", "OPENAI_API_KEY"):
            if env.get(name):
                output = output.replace(env[name], "[redacted]")
        path = OUTPUT / ("B15_command_failure_" + uuid4().hex + ".log")
        path.write_text(output, encoding="utf-8")
        raise RuntimeError("Command failed; sanitized diagnostics: " + str(path))
    return result.stdout.strip()


def request(base, path, *, body=None, token=None, expected=200):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        with HTTP.open(req, timeout=10) as response:
            status, raw = response.status, response.read()
    except HTTPError as error:
        status, raw = error.code, error.read()
    if status != expected:
        raise RuntimeError(f"Unexpected HTTP status {status}; expected {expected}")
    return json.loads(raw) if raw[:1] in (b"{", b"[") else raw


def wait_ready(base, status, *, component=None):
    deadline = monotonic() + 60
    while monotonic() < deadline:
        try:
            value = request(base, "/ready", expected=status)
            if component is None or value["checks"][component] is False:
                assert request(base, "/health") == {"status": "ok"}
                return value
        except (RuntimeError, URLError, TimeoutError):
            sleep(.5)
    raise RuntimeError("Readiness transition did not complete")


def main():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    identity = uuid4().hex
    project = "b15-" + identity[:12]
    env_file = OUTPUT / f"B15_compose_{identity}.env"
    report_path = OUTPUT / f"B15_compose_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.json"
    report = {"project": project, "checks": [], "paid_llm_calls": 0}
    values = {}
    for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    values.update(POSTGRES_USER="pycode", POSTGRES_DB="pycode", POSTGRES_PASSWORD=secrets.token_hex(24),
        PYCODE_JWT_SECRET=secrets.token_urlsafe(48), PYCODE_METRICS_TOKEN=secrets.token_urlsafe(48),
        OPENAI_API_KEY="", PYCODE_API_PORT="0", PYCODE_WEB_PORT="0",
        PYCODE_BACKEND_IMAGE="pycode-backend:" + project, PYCODE_FRONTEND_IMAGE="pycode-frontend:" + project)
    env = {**os.environ, **values, "PYCODE_ENV_FILE": env_file.as_posix()}
    compose = ["docker", "compose", "--project-name", project, "--env-file", str(env_file), "--file", str(ROOT / "compose.yaml")]
    owned_file, started = False, False

    def command(*args, timeout=240):
        return run([*compose, *args], env=env, timeout=timeout)

    def endpoint(service, port):
        address = command("port", service, str(port)).splitlines()[0]
        assert address.startswith("127.0.0.1:"), "Smoke service must bind only to loopback"
        return "http://" + address

    try:
        # Refuse a remote daemon before any build or service mutation.
        context = env.get("DOCKER_CONTEXT")
        host = (json.loads(run(["docker", "context", "inspect", context, "--format", "{{json .Endpoints.docker.Host}}"], env=env))
                if context else env.get("DOCKER_HOST") or json.loads(run(
                    ["docker", "context", "inspect", "--format", "{{json .Endpoints.docker.Host}}"], env=env)))
        if not host.startswith(("npipe://", "unix://")):
            raise RuntimeError("This smoke test requires a local Docker socket")
        for resource, listing in (("container", ["ps", "-aq"]), ("volume", ["volume", "ls", "-q"]), ("network", ["network", "ls", "-q"])):
            if run(["docker", *listing, "--filter", "label=com.docker.compose.project=" + project], env=env):
                raise RuntimeError("Generated Compose project already owns a " + resource)
        with env_file.open("x", encoding="utf-8") as stream:
            owned_file = True
            stream.write("\n".join(f"{key}={value}" for key, value in values.items()) + "\n")
        if env.get("GITHUB_ACTIONS") == "true":
            for key in ("POSTGRES_PASSWORD", "PYCODE_JWT_SECRET", "PYCODE_METRICS_TOKEN"):
                print("::add-mask::" + values[key], flush=True)
        command("config", "--quiet")
        command("build", "backend", "frontend", timeout=900)
        report["checks"].append("configuration_and_images")
        started = True
        command("up", "--detach", "--wait", "--wait-timeout", "180", timeout=240)
        base, web = endpoint("backend", 8000), endpoint("frontend", 80)
        report["checks"].append("empty_database_migrated_and_stack_healthy")
        wait_ready(base, 200)
        assert request(web, "/")
        command("exec", "-T", "worker", "python", "-m", "celery", "-A", "backend.app.worker:app", "inspect", "ping", "--timeout", "10")
        code = ("import os; from pathlib import Path; "
                "assert os.getuid()==10001; "
                "assert all(not Path('/app',p).exists() for p in ['.env','docs_v3.0','AGENTS.md','.aws']); "
                "assert b'check_config' not in Path('/proc/1/cmdline').read_bytes()")
        command("exec", "-T", "backend", "python", "-c", code)
        marker = ".b15-write-" + identity
        command("exec", "-T", "worker", "python", "-c",
                "import os; from pathlib import Path; p=Path(os.environ['PYCODE_WORKSPACE_ROOT'])/" + repr(marker) + "; p.write_text('ok'); p.unlink()")
        report["checks"].append("worker_ping_nonroot_exec_and_workspace")

        password = secrets.token_urlsafe(32)
        credentials = {"account_name": "b15-alice", "password": password}
        alice = request(base, "/api/v1/auth/register", body=credentials, expected=201)
        token = request(base, "/api/v1/auth/login", body=credentials)["access_token"]
        request(base, "/api/v1/auth/register", body={**credentials, "account_name": "b15-bob"}, expected=201)
        bob = request(base, "/api/v1/auth/login", body={**credentials, "account_name": "b15-bob"})["access_token"]
        resource = request(base, "/api/v1/projects", body={"name": "deployment-preserved", "repo_url": "https://github.com/example/demo.git"}, token=token, expected=201)
        request(base, "/api/v1/auth/me", expected=401)
        request(base, "/api/v1/projects/" + resource["id"], token=bob, expected=404)
        request(base, "/metrics", expected=401)
        assert request(base, "/metrics", token=values["PYCODE_METRICS_TOKEN"])
        assert request(web, "/api/v1/auth/me", token=token)["id"] == alice["id"]
        legacy_code = """from backend.app.db.session import DatabaseSessionManager
from backend.app.repositories import SqlAlchemyUnitOfWork
manager=DatabaseSessionManager()
with manager.open_session() as session:
    uow=SqlAlchemyUnitOfWork(session)
    with uow.transaction():
        project=uow.projects.create('deployment-legacy','https://github.com/example/demo.git')
        print(project.id)
manager.dispose()
"""
        legacy = str(UUID(command("exec", "-T", "backend", "python", "-c", legacy_code)))
        request(base, "/api/v1/projects/" + legacy, token=token, expected=404)
        report["checks"].append("authentication_ownership_and_metrics")

        command("run", "--rm", "--no-deps", "migrate")
        command("up", "--detach", "--no-deps", "--no-build", "--force-recreate", "--wait", "--wait-timeout", "180", "backend", "worker", "beat")
        command("up", "--detach", "--no-deps", "--no-build", "--force-recreate", "--wait", "--wait-timeout", "180", "frontend")
        base, web = endpoint("backend", 8000), endpoint("frontend", 80)
        assert request(base, "/api/v1/auth/me", token=token)["id"] == alice["id"]
        assert request(base, "/api/v1/projects/" + resource["id"], token=token)["id"] == resource["id"]
        request(base, "/api/v1/projects/" + legacy, token=token, expected=404)
        assert request(web, "/api/v1/auth/me", token=token)["id"] == alice["id"]
        report["checks"].append("repeat_migration_recreate_preserves_users_projects_and_legacy_ownership")

        for service, component in (("redis", "redis"), ("postgres", "postgresql")):
            command("stop", service)
            wait_ready(base, 503, component=component)
            command("start", service)
            wait_ready(base, 200)
            report["checks"].append(service + "_outage_liveness_and_recovery")
        assert request(base, "/api/v1/projects/" + resource["id"], token=token)["id"] == resource["id"]
        report["status"] = "passed"
    except Exception as exc:
        report["status"], report["error_type"] = "failed", type(exc).__name__
        if isinstance(exc, RuntimeError):
            report["error"] = str(exc)
        print("Compose smoke failed; see the private report.", file=sys.stderr)
    finally:
        try:
            if started:
                command("down", "--volumes", "--remove-orphans", "--timeout", "20")
                assert not run(["docker", "ps", "-aq", "--filter", "label=com.docker.compose.project=" + project], env=env)
                assert not run(["docker", "volume", "ls", "-q", "--filter", "label=com.docker.compose.project=" + project], env=env)
                report["cleanup"] = "own_project_and_volumes_removed"
        except Exception as exc:
            report["status"], report["cleanup_error_type"] = "failed", type(exc).__name__
        finally:
            if owned_file:
                assert env_file.resolve().parent == OUTPUT.resolve()
                env_file.unlink()
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print("Compose smoke report:", report_path)
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
