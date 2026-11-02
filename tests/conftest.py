"""Never discover developer model credentials or contact external model hosts in tests."""
import os
from pathlib import Path

import httpx
import pytest

# Collection imports create the API application before autouse fixtures execute.
os.environ['PYTHON_DOTENV_DISABLED'] = '1'
os.environ['PYCODE_EMBEDDING_ENABLED'] = 'false'


@pytest.fixture(autouse=True)
def isolate_model_credentials_and_transport(monkeypatch):
    import pycode.llm_client as llm
    real_env = Path(__file__).resolve().parents[1] / '.env'
    parse = llm._parse_env_file

    def safe_parse(path):
        # Temporary .env fixtures remain supported; the developer file is excluded.
        return {} if Path(path).resolve() == real_env else parse(path)

    monkeypatch.setattr(llm, '_parse_env_file', safe_parse)
    send = httpx.HTTPTransport.handle_request
    async_send = httpx.AsyncHTTPTransport.handle_async_request

    def guarded_send(transport, request):
        if request.url.host not in {'localhost', '127.0.0.1', '::1'}:
            raise AssertionError('External HTTP transport is forbidden in automated tests; use MockTransport.')
        return send(transport, request)

    async def guarded_async_send(transport, request):
        if request.url.host not in {'localhost', '127.0.0.1', '::1'}:
            raise AssertionError('External HTTP transport is forbidden in automated tests; use MockTransport.')
        return await async_send(transport, request)

    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', guarded_send)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', guarded_async_send)
