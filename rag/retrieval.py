"""Cited retrieval over a user's knowledge base.

``retrieve(user, query, k=5)`` is the integration entry point: cosine
similarity between the query embedding and the user's stored chunk
embeddings, returning up to ``k`` source items in exactly the contract
shape ``{document_id, chunk_id, title, snippet, score}``.

User isolation is enforced at the queryset level -- chunks are joined to
documents owned by the requesting user -- so another user's documents
can never appear in results, even when their content is identical.
"""

import math

from assistant.models import Chunk, Document

from .embeddings import EmbeddingClient, default_embedder

SNIPPET_LENGTH = 280


def available() -> bool:
    """Whether retrieval can embed queries with the deployment's embedder.

    ``False`` (no OPENAI_API_KEY) means the feature is unavailable, not
    that a search found nothing: ``retrieve`` returns an empty list in
    both cases, and this flag lets callers report the honest difference.
    """
    return default_embedder() is not None


def retrieve(
    user,
    query: str,
    k: int = 5,
    *,
    notebook_id: int | None = None,
    embedder: EmbeddingClient | None = None,
) -> list[dict]:
    """Return the top ``k`` source items for ``query`` from ``user``'s documents.

    Each item is ``{document_id, chunk_id, title, snippet, score}`` with
    ``score`` the cosine similarity between the query and chunk vectors
    (rounded to 4 decimals). Returns ``[]`` when no embedder is
    configured -- check :func:`available` to distinguish "unavailable"
    from "no matches".

    ``notebook_id`` narrows the same user-scoped query to one notebook's
    documents; ``None`` searches the whole knowledge base. It can only
    ever shrink the candidate set -- a notebook can never surface
    documents the user could not already retrieve.
    """
    client = embedder if embedder is not None else default_embedder()
    if client is None:
        return []
    query_vector = client.embed([query])[0]

    # Strict isolation: the join to document__user is the only access path.
    scope = {'document__user': user, 'document__status': Document.Status.READY}
    if notebook_id is not None:
        scope['document__notebook_id'] = notebook_id
    chunks = (
        Chunk.objects.filter(**scope)
        .select_related('document')
        .order_by('document_id', 'index')
    )
    scored: list[tuple[float, Chunk]] = []
    for chunk in chunks:
        if not chunk.embedding:
            continue
        score = _cosine_similarity(query_vector, chunk.embedding)
        # Zero or degenerate similarity means no shared signal at all --
        # such a chunk is not a match and must not surface as a citation.
        if score is not None and score > 0.0:
            scored.append((score, chunk))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [_source_item(score, chunk) for score, chunk in scored[:k]]


def _source_item(score: float, chunk: Chunk) -> dict:
    content = chunk.content
    snippet = (
        content
        if len(content) <= SNIPPET_LENGTH
        else content[:SNIPPET_LENGTH].rstrip() + '…'
    )
    return {
        'document_id': chunk.document_id,
        'chunk_id': chunk.pk,
        'title': chunk.document.original_filename,
        'snippet': snippet,
        'score': round(score, 4),
    }


def _cosine_similarity(a: list[float], b: list[float]) -> float | None:
    """Cosine of two vectors, or ``None`` when either is empty/degenerate."""
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return None
    return dot / (norm_a * norm_b)
