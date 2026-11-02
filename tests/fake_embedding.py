"""Offline deterministic Embedding double shared by Core and Worker tests."""
import hashlib
from pycode.embedding import EmbeddingResult


class FakeEmbeddingProvider:
    provider = "fake"

    def __init__(self, model="fake-v1", dimension=8, failures=None, transform=None):
        self.model, self.dimension = model, dimension
        self.calls = []
        self.failures = dict(failures or {})
        self.transform = transform

    def embed_documents(self, texts):
        self.calls.append(list(texts))
        if len(self.calls) in self.failures:
            raise self.failures[len(self.calls)]
        vectors = []
        for text in texts:
            raw = hashlib.shake_256((self.model + text).encode()).digest(self.dimension * 2)
            vectors.append([(int.from_bytes(raw[i:i + 2], "little") - 32768) / 32768
                            for i in range(0, len(raw), 2)])
        result = EmbeddingResult(self.provider, self.model, self.dimension, len(texts), vectors,
                                 batch_sizes=(len(texts),))
        return self.transform(result) if self.transform else result

    def embed_query(self, text):
        return self.embed_documents([text])
