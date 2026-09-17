"""Deterministic, offline embedder fakes for RAG tests. Zero network."""

VOCAB_DIMENSIONS = 8

DEFAULT_FAKE_VOCAB = {
    'wafer': 0,
    'yield': 1,
    'etch': 2,
    'deposition': 3,
    'plasma': 4,
    'defect': 5,
}


class FakeEmbedder:
    """Fixed-vocabulary embedder: each known token adds a unit vector.

    Multi-token texts sum their tokens' unit vectors, so cosine similarity
    is 1 for identical texts, 0 for disjoint vocabularies, and in between
    otherwise. Texts with no vocabulary tokens produce the zero vector,
    which retrieval skips (degenerate direction).

    ``embed_calls`` records every batch passed to ``embed`` so tests can
    assert how many embedding calls were made (embedding cache behavior).
    """

    model_name = 'fake-embedder'

    def __init__(self, vocab: dict[str, int] | None = None):
        self.vocab = dict(vocab if vocab is not None else DEFAULT_FAKE_VOCAB)
        self.embed_calls: list[list[str]] = []

    def embed(self, texts) -> list[list[float]]:
        batch = list(texts)
        self.embed_calls.append(batch)
        return [self._embed_one(text) for text in batch]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * VOCAB_DIMENSIONS
        for token in text.lower().split():
            index = self.vocab.get(token)
            if index is not None:
                vector[index % VOCAB_DIMENSIONS] += 1.0
        return vector
