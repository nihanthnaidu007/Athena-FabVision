"""First-run knowledge-base seeding for self-registered accounts (v1.1 #3).

A brand-new account starts with an empty knowledge base, and an empty
product has nothing to demonstrate itself with. Every new user is
seeded with two starter documents so the first visit already shows what
the product does:

- ``welcome-to-athena-fabvision.md`` -- a short how-to note that flows
  through the exact same ingest pipeline as a manual upload (chunked
  and embedded when an embedder is configured, honestly ``pending``
  without one).
- ``wafer_map_example.csv`` -- the bundled example wafer, stored as a
  storage-only document: the wafer analyzer reads it from disk via its
  existing ``path=`` parameter, so there is no text to chunk or embed.

Seeding is best-effort and idempotent. It never blocks account
creation: a seed item that cannot be stored is logged and skipped (a
stored-but-failed ingest surfaces on the document row, matching the
upload pipeline's honest-failure contract). The guard is the seed
content's sha256 -- the same field an upload records -- so re-running
the seed for a user who already has it (or uploaded the identical file)
never duplicates documents. Seeded rows are ordinary user-scoped
Documents: private to the account they were seeded into.
"""

import hashlib
import logging
from pathlib import Path

from django.core.files.base import ContentFile

from assistant.models import Document
from rag.ingestion import EmbeddingClient, ingest_document

logger = logging.getLogger(__name__)

WELCOME_FILENAME = 'welcome-to-athena-fabvision.md'
EXAMPLE_WAFER_FILENAME = 'wafer_map_example.csv'

# Inline rather than a packaged file: the welcome note ships with the
# code that renders it, so no deployment can end up without it.
WELCOME_TEXT = """# Welcome to Athena FabVision

Athena FabVision is your fab-process assistant: ask questions in plain
language and get answers grounded in your own documents, with citations
you can click.

## What this assistant can do

- **Answer from your knowledge base.** Upload lecture notes, datasheets,
  or lab summaries (as .txt, .md, or .pdf) and every answer cites the
  documents it drew from, so you can verify as you read.
- **Analyze wafers.** Paste a wafer-bin CSV (columns: wafer_id, x, y,
  bin) or point the analyzer at a stored one, and it returns yield,
  bin distribution, and pattern scores (edge ring, center hotspots).
- **Triage process excursions.** Share lot metrics and the triage tool
  walks the alarm rules with you.

## Your starter documents

This note and the example wafer CSV (wafer_map_example.csv) were placed
in your knowledge base when your account was created -- they are yours
alone, visible to no other user. Try asking: "What can you tell me
about wafer yield?" to see a cited answer built from this note.

## Tips

- Ask follow-ups; the assistant remembers the conversation.
- When an answer cites a source, open it -- the citation shows the
  exact passage the answer used.
- Deleting a document removes it from future answers; past answers
  keep the citations they already showed.
"""


def _read_example_wafer_csv() -> bytes | None:
    """Read the bundled example CSV from the fabtools package.

    The examples directory ships with the repo checkout the server runs
    from; a missing file is a packaging error, logged and skipped (the
    rest of the seed still lands).
    """
    import fabtools

    path = Path(fabtools.__file__).resolve().parent / 'examples' / EXAMPLE_WAFER_FILENAME
    try:
        return path.read_bytes()
    except OSError:
        logger.exception('First-run seed: cannot read bundled example CSV at %s.', path)
        return None


def _store_document(
    user, *, filename: str, file_type: str, content: bytes
) -> Document | None:
    """Guard + create + store one seed document; ``None`` when already present.

    The duplicate-seed guard matches on sha256 -- the same field every
    upload records -- so re-running the seed (or a user uploading the
    identical file) can never produce a second copy.
    """
    content_digest = hashlib.sha256(content).hexdigest()
    if Document.objects.for_user(user).filter(sha256=content_digest).exists():
        return None
    document = Document.objects.create(
        user=user,
        original_filename=filename,
        file_type=file_type,
        sha256=content_digest,
    )
    document.file.save(filename, ContentFile(content), save=True)
    return document


def seed_new_user_knowledge_base(
    user, *, embedder: EmbeddingClient | None = None
) -> list[Document]:
    """Store and ingest the starter documents into ``user``'s knowledge base.

    Safe to call more than once: seeded content is detected by hash and
    never duplicated ("seed runs once per user"). Returns the documents
    this call actually created -- an empty list means everything was
    already there.
    """
    seeded: list[Document] = []

    welcome = _store_document(
        user,
        filename=WELCOME_FILENAME,
        file_type='md',
        content=WELCOME_TEXT.encode('utf-8'),
    )
    if welcome is not None:
        # Same pipeline as a manual upload: ready with chunks when an
        # embedder is configured, honestly pending in zero-key mode.
        ingest_document(welcome, embedder=embedder)
        seeded.append(welcome)

    csv_content = _read_example_wafer_csv()
    if csv_content is not None:
        csv_document = _store_document(
            user,
            filename=EXAMPLE_WAFER_FILENAME,
            file_type='csv',
            content=csv_content,
        )
        if csv_document is not None:
            # Storage-only by design: the wafer analyzer reads this file
            # from disk via its path= parameter, so it is usable the
            # moment it is stored -- there is no text to chunk or embed.
            csv_document.status = Document.Status.READY
            csv_document.save(update_fields=['status', 'updated_at'])
            seeded.append(csv_document)

    if seeded:
        logger.info('First-run seed: %d document(s) created for user %s.', len(seeded), user.pk)
    return seeded
