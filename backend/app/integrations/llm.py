"""Backend-owned OpenAI transport configuration for the protected Agent Core."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from backend.app.config import LLMTransportSettings
from pycode.llm_client import (
    API_KEY_ENV,
    API_TYPE_CHAT,
    API_TYPE_ENV,
    API_TYPE_RESPONSES,
    BASE_URL_ENV,
    LLM_ERROR_DEPENDENCY_MISSING,
    LLM_ERROR_MISSING_CONFIG,
    LLM_ERROR_UNSUPPORTED_API_TYPE,
    LLMError,
    MODEL_ENV,
    classify_llm_error,
    load_llm_settings,
)


logger = logging.getLogger("pycode.backend.llm")


@dataclass
class BackendOpenAIClient:
    """Preserve Core requests while bounding SDK request I/O and retries.

    The SDK may retry one HTTP request. The Backend never retries an entire
    ``adapter.run_agent()`` call or AgentRun.
    """

    transport: LLMTransportSettings
    model: str | None = None
    env_file: Path | None = None

    def generate(self, prompt: str) -> str:
        values = load_llm_settings(self.env_file)
        api_key = _setting(values, API_KEY_ENV)
        if not api_key:
            raise LLMError(
                f"Missing {API_KEY_ENV}. Set it before running PyCode LLM commands.",
                category=LLM_ERROR_MISSING_CONFIG,
            )
        model = _clean(self.model) or _setting(values, MODEL_ENV)
        if not model:
            raise LLMError(
                f"Missing {MODEL_ENV}. Set it before running PyCode LLM commands.",
                category=LLM_ERROR_MISSING_CONFIG,
            )
        base_url = _setting(values, BASE_URL_ENV)
        api_type = (_setting(values, API_TYPE_ENV) or API_TYPE_RESPONSES).lower()

        try:
            from httpx import Timeout
            from openai import OpenAI
        except ImportError as exc:
            raise LLMError(
                "Missing dependency 'openai'. Install project requirements first.",
                category=LLM_ERROR_DEPENDENCY_MISSING,
            ) from exc

        kwargs = {
            "api_key": api_key,
            "max_retries": self.transport.max_retries,
            "timeout": Timeout(
                self.transport.read_timeout_seconds,
                connect=self.transport.connect_timeout_seconds,
                read=self.transport.read_timeout_seconds,
                write=self.transport.write_timeout_seconds,
                pool=self.transport.pool_timeout_seconds,
            ),
        }
        if base_url:
            kwargs["base_url"] = base_url
        client = OpenAI(**kwargs)
        try:
            if api_type == API_TYPE_CHAT:
                response = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                )
                return response.choices[0].message.content or ""
            if api_type != API_TYPE_RESPONSES:
                raise LLMError(
                    f"Unsupported {API_TYPE_ENV}: {api_type}. Use 'responses' or 'chat'.",
                    category=LLM_ERROR_UNSUPPORTED_API_TYPE,
                )
            response = client.responses.create(model=model, input=prompt)
            output_text = getattr(response, "output_text", None)
            return str(output_text) if output_text else str(response)
        except LLMError:
            raise
        except Exception as exc:
            raise LLMError(str(exc), category=classify_llm_error(exc)) from exc


def backend_llm_client_factory_from_environment(
) -> Callable[[str | None], BackendOpenAIClient]:
    transport = LLMTransportSettings.from_environment()
    logger.info(
        "LLM transport configured connect=%s read=%s write=%s pool=%s max_retries=%s",
        transport.connect_timeout_seconds,
        transport.read_timeout_seconds,
        transport.write_timeout_seconds,
        transport.pool_timeout_seconds,
        transport.max_retries,
    )
    return lambda model=None: BackendOpenAIClient(transport, model=model)


def _setting(values: dict[str, str], key: str) -> str | None:
    return _clean(values.get(key))


def _clean(value: str | None) -> str | None:
    stripped = value.strip() if value is not None else ""
    return stripped or None
