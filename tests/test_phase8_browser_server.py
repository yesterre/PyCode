"""The browser fixture must start without an existing .tmp or developer configuration."""
import os
from pathlib import Path
import subprocess
import sys


def test_browser_server_initializes_in_a_clean_directory(tmp_path):
    root = Path(__file__).resolve().parents[1]
    assert not (tmp_path / '.tmp').exists()
    # A developer dotenv must not activate external providers or affect app startup.
    (tmp_path / '.env').write_text(
        'PYCODE_EMBEDDING_ENABLED=true\nPYCODE_RETRIEVAL_MODE=vector\n'
        'PYCODE_RATE_WINDOW_SECONDS=invalid-personal-setting\n', encoding='utf-8')
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(('PYCODE_', 'OPENAI_', 'TEST_'))
           and key not in {'DATABASE_URL', 'REDIS_URL', 'PYTHON_DOTENV_DISABLED'}}
    env.update(PYTHONPATH=os.pathsep.join((str(root), str(root / 'tests'))),
               PYCODE_EMBEDDING_ENABLED='true', PYCODE_RETRIEVAL_MODE='vector')
    result = subprocess.run([sys.executable, '-c', '''
import os
from pathlib import Path
import phase8_browser_server as server
from fastapi.testclient import TestClient
from backend.app.repositories import InMemoryPersistence

assert server._temporary_root == Path.cwd() / '.tmp'
assert server._source.is_dir()
assert os.environ['PYTHON_DOTENV_DISABLED'] == '1'
assert os.environ['PYCODE_EMBEDDING_ENABLED'] == 'false'
assert os.environ['PYCODE_RETRIEVAL_MODE'] == 'keyword'
assert isinstance(server.app.state.persistence, InMemoryPersistence)
assert isinstance(server.app.state.realtime_redis, server.FakeRedis)
assert server.ControlledLLM().generate('test') == 'Controlled repository answer.'
with TestClient(server.app) as client:
    assert client.get('/health').json() == {'status': 'ok'}
'''], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / '.tmp').is_dir()
