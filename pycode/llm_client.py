import os
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable


DEFAULT_ENV_FILE = ".env"
API_KEY_ENV = "OPENAI_API_KEY"
BASE_URL_ENV = "OPENAI_BASE_URL"
MODEL_ENV = "OPENAI_MODEL"
API_TYPE_ENV = "OPENAI_API_TYPE"
API_TYPE_RESPONSES = "responses"
API_TYPE_CHAT = "chat"
LLM_ERROR_MISSING_CONFIG = "missing_config"
LLM_ERROR_UNSUPPORTED_API_TYPE = "unsupported_api_type"
LLM_ERROR_DEPENDENCY_MISSING = "dependency_missing"
LLM_ERROR_AUTH_FAILED = "auth_failed"
LLM_ERROR_RATE_LIMITED = "rate_limited"
LLM_ERROR_TIMEOUT = "timeout"
LLM_ERROR_INVALID_RESPONSE = "invalid_response"
LLM_ERROR_UNKNOWN = "unknown"


class LLMError(RuntimeError):
    def __init__(self, message: str, *, category: str = LLM_ERROR_UNKNOWN) -> None:
        super().__init__(message)
        self.category = category


def classify_llm_error(exc: BaseException) -> str:
    category = getattr(exc, "category", None)
    if isinstance(category, str) and category:
        return category

    status_code = getattr(exc, "status_code", None)
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    combined = f"{name} {message}"

    if status_code in {401, 403} or "auth" in combined or "unauthorized" in combined:
        return LLM_ERROR_AUTH_FAILED
    if status_code == 429 or "rate" in combined or "quota" in combined:
        return LLM_ERROR_RATE_LIMITED
    if "timeout" in combined or "timed out" in combined:
        return LLM_ERROR_TIMEOUT
    if "invalid response" in combined:
        return LLM_ERROR_INVALID_RESPONSE
    return LLM_ERROR_UNKNOWN


@runtime_checkable
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
            raise LLMError(
                f"Missing {self.api_key_env}. Set it in your shell or .env before running PyCode LLM commands.",
                category=LLM_ERROR_MISSING_CONFIG,
            )
        model = _explicit_model(self.model) or _setting(settings, self.model_env)
        if not model:
            raise LLMError(
                f"Missing {self.model_env}. Set it in your shell or .env, "
                "or pass --model before running PyCode LLM commands.",
                category=LLM_ERROR_MISSING_CONFIG,
            )
        base_url = _setting(settings, self.base_url_env)
        api_type = (_setting(settings, self.api_type_env) or API_TYPE_RESPONSES).lower()

        try:
            from openai import OpenAI
        except ImportError as exc:
            raise LLMError(
                "Missing dependency 'openai'. Install project requirements first.",
                category=LLM_ERROR_DEPENDENCY_MISSING,
            ) from exc

        client_kwargs = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        client = OpenAI(**client_kwargs)

        try:
            if api_type == API_TYPE_CHAT:
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                )
                return response.choices[0].message.content or ""

            if api_type != API_TYPE_RESPONSES:
                raise LLMError(
                    f"Unsupported {self.api_type_env}: {api_type}. Use 'responses' or 'chat'.",
                    category=LLM_ERROR_UNSUPPORTED_API_TYPE,
                )

            response = client.responses.create(
                model=model,
                input=prompt,
            )
            output_text = getattr(response, "output_text", None)
            if output_text:
                return str(output_text)
            return str(response)
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(str(exc), category=classify_llm_error(exc)) from exc


def load_llm_settings(env_file: Path | None = None) -> dict[str, str]:
    """Load LLM settings from .env and merge shell environment over it."""
    settings: dict[str, str] = {}
    env_path = env_file or _find_env_file(Path.cwd())
    if env_path is not None and env_path.exists() and env_path.is_file():
        settings.update(_parse_env_file(env_path))

    for key in (API_KEY_ENV, BASE_URL_ENV, MODEL_ENV, API_TYPE_ENV):
        value = os.environ.get(key)
        if value:
            settings[key] = value
    return settings


def _find_env_file(start: Path) -> Path | None:
    current = start.resolve()
    for directory in [current, *current.parents]:
        candidate = directory / DEFAULT_ENV_FILE
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


def _setting(settings: dict[str, str], key: str) -> str | None:
    value = settings.get(key)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _explicit_model(model: str | None) -> str | None:
    if model is None:
        return None
    stripped = model.strip()
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
