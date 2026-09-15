from types import SimpleNamespace

import httpx
import pytest

from backend.app.config import DatabaseSettings, LLMTransportSettings
from backend.app.core.errors import BackendError
from backend.app.db.session import DatabaseSessionManager
from backend.app.infrastructure.celery import create_celery_app
from backend.app.infrastructure.projects import RedisProjectLockBackend
from backend.app.infrastructure.redis import RedisSettings
from backend.app.integrations.llm import BackendOpenAIClient
from backend.app.integrations.pycode import PyCodeAdapter
from pycode.llm_client import LLMError, LLM_ERROR_TIMEOUT


def test_timeout_settings_read_validated_environment(monkeypatch):
    values = {
        "PYCODE_DB_CONNECT_TIMEOUT_SECONDS": "7",
        "PYCODE_LLM_CONNECT_TIMEOUT_SECONDS": "2",
        "PYCODE_LLM_READ_TIMEOUT_SECONDS": "90",
        "PYCODE_LLM_WRITE_TIMEOUT_SECONDS": "11",
        "PYCODE_LLM_POOL_TIMEOUT_SECONDS": "12",
        "PYCODE_LLM_MAX_RETRIES": "1",
        "REDIS_URL": "redis://localhost:6379/9",
        "PYCODE_REDIS_CONNECT_TIMEOUT_SECONDS": "3",
        "PYCODE_REDIS_SOCKET_TIMEOUT_SECONDS": "4",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    assert DatabaseSettings.from_environment().connect_timeout_seconds == 7
    llm = LLMTransportSettings.from_environment()
    assert (llm.connect_timeout_seconds, llm.read_timeout_seconds) == (2, 90)
    assert (llm.write_timeout_seconds, llm.pool_timeout_seconds, llm.max_retries) == (11, 12, 1)
    redis = RedisSettings.from_environment()
    assert (redis.connect_timeout_seconds, redis.socket_timeout_seconds) == (3, 4)


def test_redis_timeouts_reach_lock_client_and_celery_transport(monkeypatch):
    captured = {}

    class FakeRedis:
        def set(self, *args, **kwargs):
            return True

    def from_url(url, **kwargs):
        captured.update(url=url, **kwargs)
        return FakeRedis()

    monkeypatch.setattr("backend.app.infrastructure.projects.Redis.from_url", from_url)
    backend = RedisProjectLockBackend("redis://localhost:6379/8")
    assert backend.acquire("key", "token", 1000)
    assert captured == {
        "url": "redis://localhost:6379/8",
        "decode_responses": True,
        "socket_connect_timeout": 5.0,
        "socket_timeout": 5.0,
    }

    celery = create_celery_app("redis://localhost:6379/8")
    assert celery.conf.broker_connection_timeout == 5.0
    assert celery.conf.broker_transport_options["socket_connect_timeout"] == 5.0
    assert celery.conf.broker_transport_options["socket_timeout"] == 5.0
    assert celery.conf.broker_transport_options["visibility_timeout"] == 21600
    assert celery.conf.task_acks_late is True
    assert celery.conf.task_acks_on_failure_or_timeout is True
    assert celery.conf.task_reject_on_worker_lost is True
    assert celery.conf.worker_prefetch_multiplier == 1
    assert celery.conf.task_soft_time_limit is None
    assert celery.conf.task_time_limit is None


def test_postgresql_connect_timeout_reaches_sqlalchemy(monkeypatch):
    captured = {}

    class FakeEngine:
        pass

    monkeypatch.setenv("PYCODE_DB_CONNECT_TIMEOUT_SECONDS", "9")
    monkeypatch.setattr(
        "backend.app.db.session.create_engine",
        lambda url, **kwargs: captured.update(url=url, **kwargs) or FakeEngine(),
    )
    monkeypatch.setattr(
        "backend.app.db.session.sessionmaker",
        lambda **kwargs: lambda: "session",
    )
    manager = DatabaseSessionManager("postgresql+psycopg://db/test")
    assert manager.open_session() == "session"
    assert captured["connect_args"] == {"connect_timeout": 9}
    assert captured["pool_pre_ping"] is True


class _FakeOpenAI:
    def __init__(self, calls, **kwargs):
        calls.append(("client", kwargs))
        self._calls = calls
        self.responses = SimpleNamespace(create=self._responses)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._chat),
        )

    def _responses(self, **kwargs):
        self._calls.append(("responses", kwargs))
        return SimpleNamespace(output_text="answer")

    def _chat(self, **kwargs):
        self._calls.append(("chat", kwargs))
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="answer"))],
        )


def test_backend_llm_client_passes_explicit_transport_and_request_semantics(monkeypatch):
    calls = []
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_API_TYPE", "responses")
    monkeypatch.setattr(
        "openai.OpenAI", lambda **kwargs: _FakeOpenAI(calls, **kwargs),
    )
    transport = LLMTransportSettings(5, 180, 30, 30, 1)

    assert BackendOpenAIClient(transport).generate("exact prompt") == "answer"
    client_kwargs = calls[0][1]
    timeout = client_kwargs["timeout"]
    assert isinstance(timeout, httpx.Timeout)
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (5, 180, 30, 30)
    assert client_kwargs["max_retries"] == 1
    assert calls[1] == (
        "responses", {"model": "test-model", "input": "exact prompt"},
    )


def test_backend_adapter_preserves_request_model_when_using_client_factory(tmp_path):
    selected_models = []

    class FakeClient:
        def generate(self, prompt):
            return "unused"

    class FakeEngine:
        def ask(self, root, question, **kwargs):
            selected_models.append((kwargs["model"], kwargs["llm_client"], question))
            return SimpleNamespace(
                answer="answer",
                retrieval=SimpleNamespace(intent="code_qa"),
                evidence=[],
            )

    client = FakeClient()
    adapter = PyCodeAdapter(
        engine=FakeEngine(),
        llm_client_factory=lambda model: selected_models.append(("factory", model)) or client,
    )
    index = tmp_path / "index.json"
    graph = tmp_path / "graph.json"
    # Artifact readers are independently covered; this test isolates transport selection.
    adapter._check_artifacts = lambda *args: (index, graph)
    adapter.ask(tmp_path, "exact question", "request-model", index_path=index, graph_path=graph)

    assert selected_models[0] == ("factory", "request-model")
    assert selected_models[1] == ("request-model", client, "exact question")


def test_backend_llm_timeout_still_maps_through_adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("OPENAI_MODEL", "test-model")
    monkeypatch.setenv("OPENAI_API_TYPE", "responses")

    class TimeoutOpenAI(_FakeOpenAI):
        def _responses(self, **kwargs):
            raise TimeoutError("provider timed out")

    monkeypatch.setattr(
        "openai.OpenAI", lambda **kwargs: TimeoutOpenAI([], **kwargs),
    )
    client = BackendOpenAIClient(LLMTransportSettings())
    with pytest.raises(LLMError) as raw:
        client.generate("prompt")
    assert raw.value.category == LLM_ERROR_TIMEOUT

    class FakeEngine:
        def run_agent(self, root, task, **kwargs):
            kwargs["llm_client"].generate("prompt")

    adapter = PyCodeAdapter(engine=FakeEngine(), llm_client=client)
    with pytest.raises(BackendError) as mapped:
        adapter.run_agent(tmp_path, "task")
    assert getattr(mapped.value, "code", None) == "model_timeout"
