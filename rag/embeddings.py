"""Embedding clients for RAG ingestion and retrieval.

:class:`EmbeddingClient` is the injection seam: production resolves it
through :func:`default_embedder` (OpenAI when a key is configured),
tests inject deterministic fakes. Nothing here requires an API key at
import time and no network call happens until ``embed`` is invoked, so
zero-key boot stays degraded-but-alive: ``default_embedder`` returns
``None`` and callers report the feature as unavailable.
"""

from collections.abc import Sequence
from typing import Protocol

from django.conf import settings

EMBEDDING_MODEL = 'text-embedding-3-small'


class EmbeddingClient(Protocol):
    """Anything that can turn text into fixed-width vectors."""

    model_name: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one embedding vector per input text, in order."""


class OpenAIEmbedder:
    """EmbeddingClient backed by the OpenAI embeddings API."""

    def __init__(self, api_key: str, model: str = EMBEDDING_MODEL):
        # Imported here, not at module top, so rag stays importable even
        # if the openai client or its dependencies fail at import time.
        from openai import OpenAI

        self._client = OpenAI(api_key=api_key)
        self.model_name = model

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        batch = list(texts)
        if not batch:
            return []
        response = self._client.embeddings.create(model=self.model_name, input=batch)
        return [item.embedding for item in response.data]


def default_embedder() -> EmbeddingClient | None:
    """The deployment's embedder, or ``None`` when no API key is configured."""
    api_key = settings.OPENAI_API_KEY  # optional_var: None without a key
    return OpenAIEmbedder(api_key=api_key) if api_key else None
