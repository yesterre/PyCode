import json
import logging
from types import SimpleNamespace

import httpx
from openai import OpenAI
import pytest

from backend.app.config import EmbeddingSettings
from backend.app.integrations.embedding import TencentEmbeddingProvider, MODEL_DIMENSIONS
from pycode.embedding import EmbeddingError, INPUT_PREFIX, prepare_inputs
from fake_embedding import FakeEmbeddingProvider


def provider(handler, **kwargs):
    settings = EmbeddingSettings(enabled=True, api_key="fake-secret", **kwargs)
    client = OpenAI(api_key=settings.api_key, base_url=settings.base_url, max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    return TencentEmbeddingProvider(settings, client=client, sleep=lambda _: None)


@pytest.mark.parametrize("model", MODEL_DIMENSIONS)
@pytest.mark.parametrize("count", [0, 1, 64, 65, 128, 129])
@pytest.mark.parametrize('batch_size', [64, 128])
def test_batches_models_response_order_and_float_encoding(model, count, batch_size):
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body['encoding_format'] == 'float'
        assert body['model'] == model
        assert len(body['input']) <= batch_size
        return httpx.Response(200, json={"model": model, "object": "list", "data": [
            {"object": "embedding", "index": i,
             "embedding": [float(body['input'][i])] * MODEL_DIMENSIONS[model]}
            for i in reversed(range(len(body['input'])))],
            "usage": {"prompt_tokens": len(body['input']), "total_tokens": len(body['input'])}})
    p = provider(handler, model=model, batch_size=batch_size)
    result = p.embed_documents([str(i) for i in range(count)])
    assert result.input_count == count
    assert [v[0] for v in result.vectors] == list(range(count))
    assert len(calls) == (count + batch_size - 1) // batch_size
    assert result.dimension == MODEL_DIMENSIONS[model]
    if count:
        assert result.usage['total_tokens'] == count
    p.close()


@pytest.mark.parametrize("failure,retries", [(429, 3), (500, 3), (503, 3), (401, 1),
                                            (400, 1), (404, 1), ('timeout', 3), ('network', 3)])
def test_retry_bound_and_no_sensitive_logs_or_exception_chain(failure, retries, caplog):
    calls = []
    def handler(request):
        calls.append(request)
        if failure == 'timeout':
            raise httpx.ReadTimeout('fake-secret private-source', request=request)
        if failure == 'network':
            raise httpx.ConnectError('fake-secret private-source', request=request)
        return httpx.Response(failure, json={'error': {'message': 'fake-secret private-source'}})
    p = provider(handler)
    with caplog.at_level(logging.DEBUG), pytest.raises(EmbeddingError) as captured:
        p.embed_query('private-source')
    assert len(calls) == retries
    assert captured.value.__context__ is None
    assert 'fake-secret' not in caplog.text and 'private-source' not in caplog.text
    p.close()


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'dimension', 'nan', 'model'])
def test_invalid_response(mutation):
    def handler(request):
        data = [{'index': i, 'embedding': [0.0] * 1024} for i in range(2)]
        model = 'kinfra-text-embedding-0.6b'
        if mutation == 'missing': data.pop()
        if mutation == 'duplicate': data[1]['index'] = 0
        if mutation == 'dimension': data[1]['embedding'].pop()
        if mutation == 'nan': data[1]['embedding'][0] = float('nan')
        if mutation == 'model': model = 'unexpected'
        return httpx.Response(200, content=json.dumps({'model': model, 'data': data}),
                              headers={'content-type': 'application/json'})
    p = provider(handler)
    with pytest.raises(EmbeddingError, match='invalid_response'):
        p.embed_documents(['one', 'two'])
    p.close()


@pytest.mark.parametrize('content', ['', 'x', 'x' * 1787, 'x' * 9000,
                                      'a\r\nb\n' * 500, '变量😀\n' * 1500, ' \t\n' * 700],
                         ids=['empty', 'short', 'boundary', 'long-line', 'crlf', 'unicode', 'whitespace'])
def test_segments_lossless_stable_offsets_and_budget(content):
    chunks = SimpleNamespace(chunks=[SimpleNamespace(id='chunk', content=content)])
    segments = prepare_inputs(chunks)
    assert segments == prepare_inputs(chunks)
    assert ''.join(s.text[len(INPUT_PREFIX):] for s in segments) == content
    end = 0
    for i, s in enumerate(segments):
        assert s.start_offset == end
        assert s.segment_index == i and s.segment_count == len(segments)
        assert len(s.text) <= 1800
        assert s.text == INPUT_PREFIX + content[s.start_offset:s.end_offset]
        end = s.end_offset
    assert end == len(content)
    assert len({s.id for s in segments}) == len(segments)


@pytest.mark.parametrize('kwargs', [{'batch_size': 129}, {'batch_size': 0},
    {'max_input_chars': 2001}, {'max_input_chars': 13}, {'max_retries': -1},
    {'timeout_seconds': float('nan')}, {'model': 'bad'}, {'base_url': 'https://key@host'},
    {'enabled': True}])
def test_configuration_hard_limits(kwargs):
    with pytest.raises(ValueError): EmbeddingSettings(**kwargs)


def test_disabled_config_and_fake_independent_of_llm(monkeypatch):
    monkeypatch.setenv('PYCODE_EMBEDDING_ENABLED', 'false')
    monkeypatch.setenv('PYCODE_EMBEDDING_API_KEY', 'not-read')
    monkeypatch.setenv('PYCODE_EMBEDDING_BATCH_SIZE', 'invalid')
    monkeypatch.setattr('backend.app.config.load_environment', lambda: pytest.fail('dotenv read'))
    assert EmbeddingSettings.from_environment() == EmbeddingSettings()
    assert 'fake-secret' not in repr(EmbeddingSettings(api_key='fake-secret'))
    p = FakeEmbeddingProvider(dimension=3)
    assert p.embed_query('x') == p.embed_query('x')
    assert p.embed_query('x').vectors != FakeEmbeddingProvider(model='other', dimension=3).embed_query('x').vectors


def test_input_rejected_before_network():
    p = provider(lambda _: pytest.fail('network request'))
    for texts in [[''], ['x' * 2001], [' '], 'abc']:
        with pytest.raises(EmbeddingError, match='invalid_input'): p.embed_documents(texts)
    p.close()


def test_usage_is_optional():
    def handler(request):
        return httpx.Response(200, json={'model': 'kinfra-text-embedding-0.6b',
            'data': [{'index': 0, 'embedding': [0.0] * 1024}]})
    p = provider(handler)
    assert p.embed_query('public test input').usage is None
    p.close()
