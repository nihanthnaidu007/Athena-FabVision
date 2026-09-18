"""Ingestion tests: chunking edges, extraction, honest ingest states.

Embeddings come from the deterministic FakeEmbedder -- no test touches
the network or needs an API key.
"""

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase

from assistant.models import Chunk, Document
from rag.ingestion import (
    IngestionError,
    UnsupportedFileType,
    chunk_text,
    extract_text,
    ingest_document,
    reingest_document,
)
from rag.tests.fakes import FakeEmbedder
from rag.tests.storage import TempMediaMixin

User = get_user_model()


def build_pdf(text: str) -> bytes:
    """A minimal one-page PDF carrying ``text``, built with computed xref offsets."""
    content = f'BT /F1 12 Tf 72 720 Td ({text}) Tj ET'.encode('ascii')
    objects = [
        b'<< /Type /Catalog /Pages 2 0 R >>',
        b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] '
        b'/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>',
        b'<< /Length %d >>\nstream\n%s\nendstream' % (len(content), content),
        b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    out = bytearray(b'%PDF-1.4\n')
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b'%d 0 obj\n' % number
        out += body
        out += b'\nendobj\n'
    xref_pos = len(out)
    out += b'xref\n0 %d\n' % (len(objects) + 1)
    out += b'0000000000 65535 f \n'
    for offset in offsets:
        out += b'%010d 00000 n \n' % offset
    out += b'trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n' % (
        len(objects) + 1,
        xref_pos,
    )
    return bytes(out)


class ChunkTextTests(SimpleTestCase):
    def test_short_text_is_one_chunk(self):
        self.assertEqual(chunk_text('short fab note'), ['short fab note'])

    def test_empty_and_whitespace_yield_no_chunks(self):
        self.assertEqual(chunk_text(''), [])
        self.assertEqual(chunk_text('   \n\t  \n'), [])

    def test_exactly_chunk_size_is_one_chunk(self):
        chunks = chunk_text('x' * 800)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(len(chunks[0]), 800)

    def test_one_char_over_spawns_two_chunks_with_overlap(self):
        chunks = chunk_text('x' * 801)
        self.assertEqual(len(chunks), 2)
        # Second window starts at size - overlap = 700, so the first 100
        # characters of chunk 2 are chunk 1's window offset by 100.
        self.assertEqual(chunks[1][:100], chunks[0][100:200])

    def test_long_text_windows_respect_size_and_overlap(self):
        text = 'abcdefghij' * 1000  # 10k chars, no whitespace
        chunks = chunk_text(text)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 800)
        self.assertEqual(chunks[0], text[:800])
        # Consecutive windows overlap by ~100 chars.
        self.assertEqual(chunks[1][:100], chunks[0][100:200])
        # The final chunk reaches the end of the text.
        self.assertTrue(text.endswith(chunks[-1]))

    def test_invalid_overlap_raises(self):
        with self.assertRaises(ValueError):
            chunk_text('abc', size=100, overlap=100)


class ExtractTextTests(SimpleTestCase):
    def test_txt_decodes_utf8_and_strips_bom(self):
        text = extract_text('notes.txt', b'\xef\xbb\xbffab notes')
        self.assertEqual(text, 'fab notes')

    def test_md_extension_supported(self):
        self.assertEqual(extract_text('readme.md', b'# yield'), '# yield')

    def test_invalid_utf8_raises(self):
        with self.assertRaises(IngestionError):
            extract_text('broken.txt', b'\xff\xfe\x00broken')

    def test_unsupported_extension_raises(self):
        with self.assertRaises(UnsupportedFileType):
            extract_text('spec.docx', b'irrelevant')

    def test_empty_text_raises(self):
        with self.assertRaises(IngestionError):
            extract_text('empty.txt', b'   ')

    def test_pdf_extraction(self):
        text = extract_text('datasheet.pdf', build_pdf('wafer yield is nominal'))
        self.assertIn('wafer yield is nominal', text)

    def test_malformed_pdf_raises(self):
        with self.assertRaises(IngestionError):
            extract_text('bad.pdf', b'not a pdf at all')


class IngestDocumentTests(TempMediaMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('alice')

    def make_document(self, content: bytes, filename: str = 'notes.txt'):
        document = Document.objects.create(
            user=self.user,
            original_filename=filename,
            file_type='txt',
            sha256='0' * 64,
        )
        document.file.save(filename, ContentFile(content), save=True)
        return document

    def test_ingest_with_fake_embedder_marks_ready_and_creates_chunks(self):
        document = self.make_document(b'wafer yield summary ' * 60)
        result = ingest_document(document, embedder=FakeEmbedder())

        self.assertEqual(result.status, Document.Status.READY)
        self.assertGreater(result.chunk_count, 0)
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.READY)
        chunks = list(document.chunks.all())
        self.assertEqual(len(chunks), result.chunk_count)
        self.assertTrue(all(chunk.embedding for chunk in chunks))
        # Chunk indices are dense and ordered.
        self.assertEqual([chunk.index for chunk in chunks], list(range(len(chunks))))

    def test_ingest_without_embedder_stays_pending(self):
        document = self.make_document(b'wafer yield summary')

        result = ingest_document(document, embedder=None)

        self.assertEqual(result.status, Document.Status.PENDING)
        self.assertTrue(result.retryable)
        self.assertIn('OPENAI_API_KEY', result.detail)
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.PENDING)
        self.assertEqual(document.chunks.count(), 0)

    def test_ingest_embedder_failure_marks_failed_and_retryable(self):
        document = self.make_document(b'wafer yield summary')

        class ExplodingEmbedder(FakeEmbedder):
            def embed(self, texts):
                raise RuntimeError('provider outage')

        result = ingest_document(document, embedder=ExplodingEmbedder())

        self.assertEqual(result.status, Document.Status.FAILED)
        self.assertTrue(result.retryable)
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.FAILED)
        self.assertIn('provider outage', document.failure_reason)
        self.assertEqual(document.chunks.count(), 0)

    def test_ingest_extraction_failure_marks_failed(self):
        document = self.make_document(b'\xff\xfe broken bytes')

        result = ingest_document(document, embedder=FakeEmbedder())

        self.assertEqual(result.status, Document.Status.FAILED)
        self.assertFalse(result.retryable)  # the file itself is bad
        document.refresh_from_db()
        self.assertNotEqual(document.failure_reason, '')

    def test_embedding_cache_reuses_vectors_for_identical_content(self):
        embedder = FakeEmbedder()
        first = ingest_document(
            self.make_document(b'wafer yield summary'), embedder=embedder
        )
        second = ingest_document(
            self.make_document(b'wafer yield summary'), embedder=embedder
        )

        self.assertEqual(first.status, Document.Status.READY)
        self.assertEqual(second.status, Document.Status.READY)
        # Identical content is embedded exactly once: the second ingest
        # reused the vectors cached on the first document's chunks.
        self.assertEqual(len(embedder.embed_calls), 1)


class ReingestDocumentTests(TempMediaMixin, TestCase):
    """The KB manager's retry path: idempotent re-ingestion of stored documents."""

    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('alice')

    def make_document(self, content: bytes, filename: str = 'notes.txt') -> Document:
        document = Document.objects.create(
            user=self.user,
            original_filename=filename,
            file_type='txt',
            sha256='0' * 64,
        )
        document.file.save(filename, ContentFile(content), save=True)
        return document

    def test_reingest_clears_stale_chunks_and_rebuilds(self):
        document = self.make_document(b'wafer yield summary ' * 60)
        # Simulate partial state: a chunk exists while the document is
        # still pending. Re-ingestion must start from a clean slate --
        # the unique (document, index) constraint would reject duplicates.
        Chunk.objects.create(document=document, index=0, content='stale', content_hash='stale')

        result = reingest_document(document, embedder=FakeEmbedder())

        self.assertEqual(result.status, Document.Status.READY)
        indexes = list(document.chunks.order_by('index').values_list('index', flat=True))
        self.assertEqual(indexes, list(range(result.chunk_count)))
        self.assertNotIn('stale', [chunk.content for chunk in document.chunks.all()])

    def test_reingest_without_embedder_stays_pending(self):
        document = self.make_document(b'wafer yield summary ' * 60)

        result = reingest_document(document)  # no embedder, no key configured

        self.assertEqual(result.status, Document.Status.PENDING)
        self.assertTrue(result.retryable)
        document.refresh_from_db()
        self.assertEqual(document.chunks.count(), 0)
