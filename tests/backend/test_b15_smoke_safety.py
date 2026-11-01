import json
from types import SimpleNamespace

from scripts.deploy import smoke


def test_smoke_refuses_remote_daemon_without_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "OUTPUT", tmp_path / "reports")
    monkeypatch.setenv("DOCKER_HOST", "ssh://remote-docker")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setattr(smoke, "run", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("No remote calls allowed")))
    assert smoke.main() == 1
    assert not list(smoke.OUTPUT.glob("*.env"))
    report = json.loads(next(smoke.OUTPUT.glob("*.json")).read_text())
    assert report["error"] == "This smoke test requires a local Docker socket"


def test_remote_context_takes_precedence_over_local_host(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "OUTPUT", tmp_path)
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote")
    def inspect_only(arguments, **kwargs):
        assert arguments[1:4] == ["context", "inspect", "remote"]
        return '"ssh://remote-docker"'
    monkeypatch.setattr(smoke, "run", inspect_only)
    assert smoke.main() == 1


def test_smoke_does_not_adopt_or_remove_an_existing_project_volume(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "OUTPUT", tmp_path / "reports")
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    commands = []

    def existing(arguments, **kwargs):
        commands.append(arguments)
        if arguments[1:4] == ["volume", "ls", "-q"]:
            return "existing-volume"
        assert arguments[1:3] == ["ps", "-aq"]
        return ""

    monkeypatch.setattr(smoke, "run", existing)
    assert smoke.main() == 1
    assert len(commands) == 2
    assert not list(smoke.OUTPUT.glob("*.env"))


def test_smoke_will_not_overwrite_or_delete_an_existing_env_file(tmp_path, monkeypatch):
    monkeypatch.setattr(smoke, "OUTPUT", tmp_path)
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    monkeypatch.setattr(smoke, "uuid4", lambda: SimpleNamespace(hex="a" * 32))
    path = tmp_path / ("B15_compose_" + "a" * 32 + ".env")
    path.write_text("existing-owner-file")
    calls = []

    def read_only(arguments, **kwargs):
        calls.append(arguments)
        assert arguments[1] in {"ps", "volume", "network"}
        return ""

    monkeypatch.setattr(smoke, "run", read_only)
    assert smoke.main() == 1
    assert path.read_text() == "existing-owner-file"
    assert len(calls) == 3


def test_failed_command_diagnostics_redact_generated_secrets(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setattr(smoke, "OUTPUT", tmp_path)
    secret = "b15-ephemeral-secret-password"
    monkeypatch.setattr(smoke.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=secret, stderr=secret))
    with pytest.raises(RuntimeError):
        smoke.run(["docker", "compose", "config", "--quiet"], env={"POSTGRES_PASSWORD": secret})
    data = next(tmp_path.glob("*.log")).read_text()
    assert secret not in data and "[redacted]" in data
