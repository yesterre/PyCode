import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


DEFAULT_MODEL = "gpt-5.4-mini"
DEFAULT_ENV_FILE = ".env"
API_KEY_ENV = "OPENAI_API_KEY"
BASE_URL_ENV = "OPENAI_BASE_URL"
MODEL_ENV = "OPENAI_MODEL"
API_TYPE_ENV = "OPENAI_API_TYPE"
API_TYPE_RESPONSES = "responses"
API_TYPE_CHAT = "chat"


class LLMClient(Protocol):
    def generate(self, prompt: str) -> str:
        """Return an LLM answer for the prepared prompt."""


@dataclass
class OpenAIResponsesClient:
    model: str | None = None
    env_file: Path | None = None
    api_key_env: str = API_KEY_ENV
    base_url_env: str = BASE_URL_ENV
    model_env: str = MODEL_ENV
    api_type_env: str = API_TYPE_ENV

    def generate(self, prompt: str) -> str:
        settings = load_llm_settings(self.env_file)
        api_key = _setting(settings, self.api_key_env)
        if not api_key:
            raise RuntimeError(
                f"Missing {self.api_key_env}. Set it in your shell or .env before running PyCode LLM commands."
            )
        model = self.model or _setting(settings, self.model_env) or DEFAULT_MODEL
        base_url = _setting(settings, self.base_url_env)
        api_type = (_setting(settings, self.api_type_env) or API_TYPE_RESPONSES).lower()

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise RuntimeError(
                "Missing dependency 'openai'. Install project requirements first."
            ) from exc

        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        client = OpenAI(**client_kwargs)

        if api_type == API_TYPE_CHAT:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content or ""

        if api_type != API_TYPE_RESPONSES:
            raise RuntimeError(
                f"Unsupported {self.api_type_env}: {api_type}. Use 'responses' or 'chat'."
            )

        response = client.responses.create(
            model=model,
            input=prompt,
        )
        output_text = getattr(response, "output_text", None)
        if output_text:
            return str(output_text)
        return str(response)


def load_llm_settings(env_file: Path | None = None) -> dict[str, str]:
    """Load LLM settings from .env and merge shell environment over it."""
    settings: dict[str, str] = {}
    env_path = env_file or Path.cwd() / DEFAULT_ENV_FILE
    if env_path.exists() and env_path.is_file():
        settings.update(_parse_env_file(env_path))

    for key in (API_KEY_ENV, BASE_URL_ENV, MODEL_ENV, API_TYPE_ENV):
        value = os.environ.get(key)
        if value:
            settings[key] = value
    return settings


def _setting(settings: dict[str, str], key: str) -> str | None:
    value = settings.get(key)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _parse_env_file(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        values[key] = _strip_env_value(value.strip())
    return values


def _strip_env_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    return value
