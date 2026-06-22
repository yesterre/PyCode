import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pycode.llm_client import OpenAIResponsesClient, load_llm_settings


def test_openai_responses_client_requires_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_TYPE", raising=False)
    client = OpenAIResponsesClient(env_file=tmp_path / ".env")

    with pytest.raises(RuntimeError, match="Missing OPENAI_API_KEY"):
        client.generate("hello")


def test_load_llm_settings_reads_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=env-file-key",
                "OPENAI_MODEL=gpt-test",
                "OPENAI_BASE_URL=https://example.test/v1",
                "OPENAI_API_TYPE=responses",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_llm_settings(env_path)

    assert settings["OPENAI_API_KEY"] == "env-file-key"
    assert settings["OPENAI_MODEL"] == "gpt-test"
    assert settings["OPENAI_BASE_URL"] == "https://example.test/v1"
    assert settings["OPENAI_API_TYPE"] == "responses"


def test_shell_environment_overrides_env_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("OPENAI_API_KEY=env-file-key\n", encoding="utf-8")
    monkeypatch.setenv("OPENAI_API_KEY", "shell-key")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    settings = load_llm_settings(env_path)

    assert settings["OPENAI_API_KEY"] == "shell-key"


def test_openai_responses_client_uses_env_file_model_and_base_url(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=env-file-key",
                "OPENAI_MODEL=gpt-test",
                "OPENAI_BASE_URL=https://example.test/v1",
            ]
        ),
        encoding="utf-8",
    )
    fake_openai = _FakeOpenAIModule()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    answer = OpenAIResponsesClient(env_file=env_path).generate("hello")

    assert answer == "ok"
    assert fake_openai.last_client.api_key == "env-file-key"
    assert fake_openai.last_client.base_url == "https://example.test/v1"
    assert fake_openai.last_client.response_args == {
        "model": "gpt-test",
        "input": "hello",
    }


def test_openai_client_can_use_chat_completions_api_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_TYPE", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=env-file-key",
                "OPENAI_MODEL=deepseek-test",
                "OPENAI_BASE_URL=https://example.test/v1",
                "OPENAI_API_TYPE=chat",
            ]
        ),
        encoding="utf-8",
    )
    fake_openai = _FakeOpenAIModule()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    answer = OpenAIResponsesClient(env_file=env_path).generate("hello")

    assert answer == "chat ok"
    assert fake_openai.last_client.response_args is None
    assert fake_openai.last_client.chat_args == {
        "model": "deepseek-test",
        "messages": [{"role": "user", "content": "hello"}],
    }


def test_openai_client_rejects_unknown_api_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    monkeypatch.delenv("OPENAI_API_TYPE", raising=False)
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "OPENAI_API_KEY=env-file-key",
                "OPENAI_API_TYPE=invalid",
            ]
        ),
        encoding="utf-8",
    )
    fake_openai = _FakeOpenAIModule()
    monkeypatch.setitem(sys.modules, "openai", fake_openai)

    with pytest.raises(RuntimeError, match="Unsupported OPENAI_API_TYPE"):
        OpenAIResponsesClient(env_file=env_path).generate("hello")


class _FakeOpenAIModule:
    def __init__(self) -> None:
        self.last_client = None

    def OpenAI(self, **kwargs):
        self.last_client = _FakeOpenAIClient(**kwargs)
        return self.last_client


class _FakeOpenAIClient:
    def __init__(self, api_key: str, base_url: str | None = None) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.response_args = None
        self.chat_args = None
        self.responses = SimpleNamespace(create=self._create)
        self.chat = SimpleNamespace(
            completions=SimpleNamespace(create=self._create_chat)
        )

    def _create(self, **kwargs):
        self.response_args = kwargs
        return SimpleNamespace(output_text="ok")

    def _create_chat(self, **kwargs):
        self.chat_args = kwargs
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content="chat ok"))
            ]
        )
