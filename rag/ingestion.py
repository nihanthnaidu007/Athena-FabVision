"""Knowledge-base ingestion: extract, chunk, embed, store.

Pipeline for an uploaded document: extract text (pypdf for PDFs, UTF-8
decode for .txt/.md), split into ~800-char chunks with ~100-char
overlap, embed through an injected :class:`EmbeddingClient`, and persist
:class:`~assistant.models.Chunk` rows scoped to the document's owner.
Embeddings are cached on ``sha256(content)`` (Chunk.content_hash): a
vector depends only on the text, so stored chunks with identical
content anywhere in the deployment are reused instead of re-embedded.

Every outcome is honest and visible: without an embedder the document
stays ``pending`` (degraded, retryable); extraction or embedding
failures mark it ``failed`` with the reason stored on the row. Nothing
is silently dropped.
"""

import hashlib
import io
import logging
from dataclasses import dataclass
from pathlib import Path

from django.db import transaction

from assistant.models import Chunk, Document

from .embeddings import EmbeddingClient, default_embedder

logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_OVERLAP = 100
SUPPORTED_EXTENSIONS = ('.txt', '.md', '.pdf')
# Storage-only documents (wafer-bin CSVs) are stored verbatim for tools that
# read files -- the wafer analyzer -- and are never chunked or embedded, so
# retrieval cannot see them. Parsing stays entirely with the analyzer.
STORAGE_ONLY_EXTENSIONS = ('.csv',)
# Everything the upload endpoint admits: knowledge-base text plus storage-only.
ACCEPTED_EXTENSIONS = SUPPORTED_EXTENSIONS + STORAGE_ONLY_EXTENSIONS


class IngestionError(Exception):
    """Text extraction failed for a submitted document (empty, unreadable, malformed)."""


class UnsupportedFileType(IngestionError):
    """The file's extension is not one of the supported knowledge-base types."""


@dataclass(frozen=True)
class IngestResult:
    """Honest outcome of one ingestion attempt."""

    document_id: int | None
    status: str  # a Document.Status value: pending | ready | failed
    chunk_count: int = 0
    detail: str = ''
    retryable: bool = False


def chunk_text(
    text: str,
    *,
    size: int = DEFAULT_CHUNK_SIZE,
    overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> list[str]:
    """Split text into sliding windows of ``size`` chars sharing ``overlap``.

    Windows advance by ``size - overlap`` chars, so consecutive chunks
    overlap by ``overlap`` characters. Whitespace-only input yields no
    chunks; every returned chunk is stripped and non-empty. ``overlap``
    must be smaller than ``size`` or no forward progress is possible.
    """
    if overlap >= size:
        raise ValueError('chunk overlap must be smaller than the chunk size')
    cleaned = text.replace('\ufeff', '').strip()
    chunks: list[str] = []
    step = size - overlap
    start = 0
    while start < len(cleaned):
        chunk = cleaned[start : start + size].strip()
        if chunk:
            chunks.append(chunk)
        if start + size >= len(cleaned):
            break
        start += step
    return chunks


def extract_text(filename: str, content: bytes) -> str:
    """Extract plain text from a supported file's bytes.

    Raises :class:`UnsupportedFileType` for unrecognized extensions and
    :class:`IngestionError` when the bytes carry no extractable text.
    """
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise UnsupportedFileType(
            f'Unsupported file type "{suffix or Path(filename).suffix}"; '
            f'accepted: {", ".join(SUPPORTED_EXTENSIONS)}.'
        )
    if suffix == '.pdf':
        text = _extract_pdf_text(content)
    else:
        text = _decode_text(content)
    if not text.strip():
        raise IngestionError(
            'No extractable text; the document may be empty or image-only.'
        )
    return text


def _decode_text(content: bytes) -> str:
    try:
        # utf-8-sig also strips a leading byte-order mark when present.
        return content.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise IngestionError(
            'File is not valid UTF-8 text; re-export it as UTF-8 and retry.'
        ) from exc


def _extract_pdf_text(content: bytes) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - pypdf is a pinned dependency
        raise IngestionError('PDF support unavailable: pypdf is not installed.') from exc
    try:
        reader = PdfReader(io.BytesIO(content))
        pages = [page.extract_text() or '' for page in reader.pages]
    except Exception as exc:
        # Third-party parsers raise a wide zoo of exceptions on malformed
        # input; the contract here is an honest failed state, not a crash.
        raise IngestionError(f'Could not read PDF: {exc}') from exc
    return '\n\n'.join(part.strip() for part in pages if part.strip())


def ingest_document(
    document: Document, *, embedder: EmbeddingClient | None = None
) -> IngestResult:
    """Extract, chunk, and embed one saved Document, updating its status.

    With no embedder available (zero-key deployment) the document stays
    ``pending`` and nothing is processed. Extraction failures mark it
    ``failed`` (not retryable -- the file itself is bad); embedding
    failures also mark it ``failed`` but retryable (transient).
    Storage-only documents (.csv) are the exception: they need no
    embeddings and are marked ``ready`` after a readability check.
    """
    suffix = Path(document.original_filename).suffix.lower()
    if suffix in STORAGE_ONLY_EXTENSIONS:
        return _ingest_storage_only(document)
    try:
        with document.file.open('rb') as stored:
            content = stored.read()
        # Extraction before the embedder check: a file that cannot be read
        # is broken regardless of whether embeddings are configured, and
        # degraded mode must not report it as "pending" forever.
        text = extract_text(document.original_filename, content)
        chunks = chunk_text(text)
    except IngestionError as exc:
        return _mark_failed(document, exc, retryable=False)
    client = embedder if embedder is not None else default_embedder()
    if client is None:
        return IngestResult(
            document_id=document.pk,
            status=Document.Status.PENDING,
            detail=(
                'Embeddings unavailable: no OPENAI_API_KEY configured. '
                'The upload is stored and will be processed once a key is set.'
            ),
            retryable=True,
        )
    try:
        vectors = _embed_chunks(chunks, client)
    except Exception as exc:
        logger.exception('Embedding failed for document %s', document.pk)
        return _mark_failed(document, exc, retryable=True, prefix='Embedding failed')

    with transaction.atomic():
        Chunk.objects.bulk_create(
            Chunk(
                document=document,
                index=index,
                content=chunk,
                embedding=vector,
                content_hash=_content_hash(chunk),
            )
            for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
        )
        document.status = Document.Status.READY
        document.failure_reason = ''
        document.save(update_fields=['status', 'failure_reason', 'updated_at'])
    return IngestResult(
        document_id=document.pk,
        status=Document.Status.READY,
        chunk_count=len(chunks),
        detail=f'{len(chunks)} chunks embedded with {client.model_name}.',
    )


def reingest_document(
    document: Document, *, embedder: EmbeddingClient | None = None
) -> IngestResult:
    """Re-run ingestion on a stored document -- the KB manager's retry path.

    Clears any stale chunks first: a clean pending/failed document has
    none, but the guard makes re-ingestion idempotent however the
    document reached its current state. Delegates to the normal pipeline
    so status, failure_reason, and chunks update exactly as on first
    upload -- including the honest ``pending`` outcome when no embedder
    is configured.
    """
    document.chunks.all().delete()
    return ingest_document(document, embedder=embedder)


def _ingest_storage_only(document: Document) -> IngestResult:
    """Mark a storage-only document ready after a readability check.

    No chunking, no embeddings: the analyzer owns CSV parsing, so a
    malformed CSV uploads fine and fails honestly (a structured error
    block) when analyzed in chat -- never silently at upload. An
    unreadable stored file is a real failure and is marked as one.
    """
    try:
        with document.file.open('rb') as stored:
            stored.read()
    except OSError as exc:
        return _mark_failed(
            document,
            IngestionError(f'could not read the stored file: {exc}'),
            retryable=False,
        )
    document.status = Document.Status.READY
    document.failure_reason = ''
    document.save(update_fields=['status', 'failure_reason', 'updated_at'])
    return IngestResult(
        document_id=document.pk,
        status=Document.Status.READY,
        chunk_count=0,
        detail=(
            'Storage-only document: stored for the wafer analyzer. It is not '
            'chunked or embedded, so chat retrieval does not search it.'
        ),
    )


def _mark_failed(
    document: Document, exc: Exception, *, retryable: bool, prefix: str = 'Ingestion failed'
) -> IngestResult:
    """Persist the failure on the document row so it is visible and retryable."""
    document.status = Document.Status.FAILED
    document.failure_reason = f'{prefix}: {exc}'
    document.save(update_fields=['status', 'failure_reason', 'updated_at'])
    logger.info(
        'Document %s failed ingestion (retryable=%s): %s', document.pk, retryable, exc
    )
    return IngestResult(
        document_id=document.pk,
        status=Document.Status.FAILED,
        detail=document.failure_reason,
        retryable=retryable,
    )


def _embed_chunks(contents: list[str], client: EmbeddingClient) -> list[list[float]]:
    """Embed chunk contents, reusing vectors cached on identical content.

    The cache key is ``sha256(content)`` (Chunk.content_hash): any stored
    chunk with the same hash -- in any document, for any user -- carries a
    reusable vector, since embeddings depend only on the text. This
    assumes one embedding model per deployment.
    """
    hashes = [_content_hash(text) for text in contents]
    if not contents:
        return []
    cached: dict[str, list[float]] = {}
    known = Chunk.objects.filter(content_hash__in=hashes).only('content_hash', 'embedding')
    for chunk in known:
        if chunk.embedding:
            cached.setdefault(chunk.content_hash, chunk.embedding)
    missing = [
        text for text, digest in zip(contents, hashes, strict=True) if digest not in cached
    ]
    if missing:
        vectors = client.embed(missing)
        for text, vector in zip(missing, vectors, strict=True):
            cached[_content_hash(text)] = vector
    return [cached[digest] for digest in hashes]


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()
