"""Manual, opt-in TokenHub contract check using three non-sensitive fixed texts.

Not part of pytest or CI. Run only after engineering acceptance and explicit --live.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='Explicitly allow a real, potentially billed API request')
    parser.add_argument('--model', choices=['kinfra-text-embedding-0.6b', 'kinfra-text-embedding-4b'])
    args = parser.parse_args(argv)
    if not args.live:
        parser.error('Real API access requires --live; no request was made.')
    from backend.app.config import EmbeddingSettings, load_environment
    from backend.app.integrations.embedding import TencentEmbeddingProvider
    from pycode.embedding import validate_result
    provider = None
    try:
        load_environment()
        settings = EmbeddingSettings.from_environment()
        if args.model:
            settings = replace(settings, model=args.model)
        provider = TencentEmbeddingProvider(settings)
        texts = ['def add(a, b): return a + b', 'A small function adds two numbers.',
                 'class Counter: pass']
        result = provider.embed_documents(texts)
        validate_result(result, len(texts), provider.provider, provider.model, provider.dimension)
        print(json.dumps({'provider': result.provider, 'model': result.model,
                          'dimension': result.dimension, 'input_count': result.input_count,
                          'all_finite': True, 'usage': result.usage, 'batch_sizes': result.batch_sizes}))
        return 0
    except Exception:
        print('Embedding smoke check failed. Check configuration and provider access; no credentials are displayed.', file=sys.stderr)
        return 1
    finally:
        if provider is not None:
            provider.close()


if __name__ == '__main__':
    raise SystemExit(main())
