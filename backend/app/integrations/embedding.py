"""TokenHub adapter. Construction is lazy; tests inject an SDK MockTransport."""
from __future__ import annotations

from contextvars import ContextVar
import logging
import time
from typing import Sequence
from dataclasses import replace

from openai import OpenAI, APIConnectionError, APITimeoutError, APIStatusError

from backend.app.config import EmbeddingSettings
from pycode.embedding import EmbeddingError, EmbeddingResult, validate_result
from pycode.agent.budget import current_budget


def query_embedding_settings(settings):
    budget = current_budget()
    if budget is None:
        return settings
    budget.consume('query_embeddings')
    return replace(settings, timeout_seconds=min(settings.timeout_seconds, budget.remaining_seconds()))


def embed_query(provider, text):
    try:
        return provider.embed_query(text)
    finally:
        if (budget := current_budget()) is not None:
            budget.check()

MODEL_DIMENSIONS = {"kinfra-text-embedding-0.6b": 1024, "kinfra-text-embedding-4b": 2560}
_request_active = ContextVar("embedding_request_active", default=False)


class _RequestLogFilter(logging.Filter):
    def filter(self, record):
        if _request_active.get():
            record.msg = "Embedding transport event (request and response details omitted)."
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


for _name in ("openai._base_client", "httpx", "httpcore.connection", "httpcore.http11",
              "httpcore.http2", "httpcore.proxy"):
    logging.getLogger(_name).addFilter(_RequestLogFilter())


class TencentEmbeddingProvider:
    provider = "tencent_tokenhub"

    def __init__(self, settings: EmbeddingSettings, *, client=None, sleep=time.sleep):
        if not settings.enabled:
            raise EmbeddingError("embedding_unavailable")
        self.settings = settings
        self.model = settings.model
        self.dimension = MODEL_DIMENSIONS[self.model]
        self._client = client
        self._sleep = sleep

    def _sdk(self):
        if self._client is None:
            self._client = OpenAI(api_key=self.settings.api_key, base_url=self.settings.base_url,
                                  timeout=self.settings.timeout_seconds, max_retries=0)
        return self._client

    def close(self):
        if self._client is not None:
            self._client.close()

    def embed_query(self, text: str) -> EmbeddingResult:
        return self.embed_documents([text])

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingResult:
        if isinstance(texts, (str, bytes)) or any(
            not isinstance(text, str) or not text.strip()
            or len(text) > self.settings.max_input_chars for text in texts
        ):
            raise EmbeddingError("embedding_invalid_input")
        vectors, sizes = [], []
        usage: dict[str, int] = {}
        for offset in range(0, len(texts), self.settings.batch_size):
            batch = list(texts[offset:offset + self.settings.batch_size])
            result = self._batch(batch)
            vectors.extend(result.vectors)
            sizes.append(len(batch))
            for key, value in (result.usage or {}).items():
                usage[key] = usage.get(key, 0) + value
        return EmbeddingResult(self.provider, self.model, self.dimension, len(texts),
                               vectors, usage or None, tuple(sizes))

    def _batch(self, texts: list[str]) -> EmbeddingResult:
        code = "embedding_failed"
        for attempt in range(self.settings.max_retries + 1):
            budget = current_budget()
            if budget is not None:
                budget.check()
            retry = False
            token = _request_active.set(True)
            try:
                response = self._sdk().embeddings.create(
                    model=self.model, input=texts, encoding_format="float",
                    **({'timeout': min(self.settings.timeout_seconds, budget.remaining_seconds())}
                       if budget is not None else {}),
                )
                data = response.data
                indices = [item.index for item in data]
                if (any(type(index) is not int for index in indices)
                        or sorted(indices) != list(range(len(texts)))):
                    raise EmbeddingError("embedding_invalid_response")
                vectors = [item.embedding for item in sorted(data, key=lambda item: item.index)]
                dimension = len(vectors[0]) if vectors else 0
                usage = None
                if getattr(response, 'usage', None) is not None:
                    usage = {key: value for key in ("prompt_tokens", "total_tokens")
                             if type(value := getattr(response.usage, key, None)) is int and value >= 0}
                result = EmbeddingResult(self.provider, response.model, dimension,
                                         len(texts), vectors, usage, (len(texts),))
                validate_result(result, len(texts), self.provider, self.model, self.dimension)
                return result
            except APITimeoutError:
                code, retry = "embedding_timeout", True
            except APIConnectionError:
                code, retry = "embedding_unavailable", True
            except APIStatusError as exc:
                retry = exc.status_code == 429 or exc.status_code >= 500
                code = ("embedding_rate_limited" if exc.status_code == 429 else
                        "embedding_authentication" if exc.status_code in {401, 403} else
                        "embedding_unavailable" if retry else "embedding_invalid_input")
            except (EmbeddingError, ValueError, TypeError, AttributeError, OverflowError):
                code = "embedding_invalid_response"
            except Exception:
                code = "embedding_invalid_response"
            finally:
                _request_active.reset(token)
            if not retry or attempt == self.settings.max_retries:
                break
            self._sleep(min(2 ** attempt, 8, budget.remaining_seconds())
                        if budget is not None else min(2 ** attempt, 8))
        # Raise outside except: no sensitive SDK exception in __context__ either.
        raise EmbeddingError(code)
