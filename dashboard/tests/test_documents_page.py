"""Knowledge-base document manager tests: auth, isolation, and lifecycles.

The manager is views over existing model state, so the interesting
behavior is transitions and scoping: pending/failed -> ready re-ingest
with an injectable embedder, delete cascading chunks, and 404s on other
users' rows. Zero network throughout -- the FakeEmbedder is
deterministic, and the no-key degraded path pins ``OPENAI_API_KEY`` to
empty so ``default_embedder()`` returns ``None``.
"""

from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse

from assistant.models import Chunk, Document
from rag.ingestion import ingest_document
from rag.tests.fakes import FakeEmbedder
from rag.tests.storage import TempMediaMixin

User = get_user_model()

DOCUMENTS_URL = 'dashboard:documents'


class DocumentsPageAuthTests(TestCase):
    """Anonymous visitors are redirected to login on every route."""

    def test_anonymous_list_get_redirects_to_login(self):
        response = self.client.get(reverse(DOCUMENTS_URL))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith(settings.LOGIN_URL))

    def test_anonymous_delete_post_redirects_to_login(self):
        response = self.client.post(reverse('dashboard:delete-document', args=[1]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith(settings.LOGIN_URL))

    def test_anonymous_reingest_post_redirects_to_login(self):
        response = self.client.post(reverse('dashboard:reingest-document', args=[1]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith(settings.LOGIN_URL))


class DocumentManagerPageTests(TempMediaMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user('alice', password='pw12345')
        cls.bob = User.objects.create_user('bob', password='pw12345')

    def setUp(self):
        super().setUp()
        self.client.force_login(self.alice)

    def make_document(
        self, user=None, content=b'wafer yield notes ' * 40, filename='notes.txt'
    ) -> Document:
        document = Document.objects.create(
            user=user or self.alice,
            original_filename=filename,
            file_type='txt',
            sha256='0' * 64,
        )
        document.file.save(filename, ContentFile(content), save=True)
        return document

    # -- Listing ---------------------------------------------------------

    def test_lists_own_documents_with_status_and_failure_reason(self):
        ready = self.make_document(filename='good.txt')
        ingest_document(ready, embedder=FakeEmbedder())
        failed = self.make_document(content=b'not a pdf at all', filename='broken.pdf')
        ingest_document(failed)  # extraction fails before the embedder is consulted

        page = self.client.get(reverse(DOCUMENTS_URL)).content.decode()
        self.assertIn('good.txt', page)
        self.assertIn('ready', page)
        self.assertIn('broken.pdf', page)
        self.assertIn('failed', page)
        self.assertIn('Could not read PDF', page)  # failure_reason surfaces verbatim

    def test_lists_only_own_documents(self):
        self.make_document(filename='mine.txt')
        self.make_document(user=self.bob, filename='theirs.txt')

        page = self.client.get(reverse(DOCUMENTS_URL)).content.decode()
        self.assertIn('mine.txt', page)
        self.assertNotIn('theirs.txt', page)

    def test_empty_state_when_no_documents(self):
        page = self.client.get(reverse(DOCUMENTS_URL)).content.decode()
        self.assertIn('knowledge base is empty', page)

    def test_chunk_count_shown_per_document(self):
        document = self.make_document(content=b'word ' * 400)  # several chunks
        result = ingest_document(document, embedder=FakeEmbedder())
        self.assertGreater(result.chunk_count, 1)

        response = self.client.get(reverse(DOCUMENTS_URL))
        self.assertEqual(response.context['documents'][0].chunk_count, result.chunk_count)

    # -- Delete ----------------------------------------------------------

    def test_delete_removes_document_and_cascades_chunks(self):
        document = self.make_document(content=b'word ' * 400)
        ingest_document(document, embedder=FakeEmbedder())
        self.assertGreater(document.chunks.count(), 0)

        response = self.client.post(reverse('dashboard:delete-document', args=[document.id]))

        self.assertRedirects(response, reverse(DOCUMENTS_URL))
        self.assertFalse(Document.objects.filter(pk=document.pk).exists())
        self.assertFalse(Chunk.objects.filter(document_id=document.pk).exists())

    def test_user_cannot_delete_another_users_document(self):
        document = self.make_document(user=self.bob)

        response = self.client.post(reverse('dashboard:delete-document', args=[document.id]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Document.objects.filter(pk=document.pk).exists())

    # -- Re-ingest -------------------------------------------------------

    def test_reingest_moves_failed_document_to_ready(self):
        class ExplodingEmbedder(FakeEmbedder):
            def embed(self, texts):
                raise RuntimeError('embeddings provider down')

        document = self.make_document()
        ingest_document(document, embedder=ExplodingEmbedder())
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.FAILED)

        with mock.patch('rag.ingestion.default_embedder', return_value=FakeEmbedder()):
            response = self.client.post(
                reverse('dashboard:reingest-document', args=[document.id]), follow=True
            )

        self.assertRedirects(response, reverse(DOCUMENTS_URL))
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.READY)
        self.assertEqual(document.failure_reason, '')
        self.assertGreater(document.chunks.count(), 0)
        self.assertIn('Re-ingested', response.content.decode())

    def test_reingest_moves_pending_document_to_ready(self):
        document = self.make_document()  # never ingested: pending by default

        with mock.patch('rag.ingestion.default_embedder', return_value=FakeEmbedder()):
            self.client.post(reverse('dashboard:reingest-document', args=[document.id]))

        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.READY)
        self.assertEqual(document.failure_reason, '')
        self.assertGreater(document.chunks.count(), 0)

    @override_settings(OPENAI_API_KEY='')
    def test_reingest_without_embeddings_key_stays_pending(self):
        document = self.make_document()

        response = self.client.post(
            reverse('dashboard:reingest-document', args=[document.id]), follow=True
        )

        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.PENDING)
        self.assertEqual(document.chunks.count(), 0)
        # Honest degraded state: the page says a key is the missing piece.
        self.assertIn('not processed yet', response.content.decode())
        self.assertIn('no OPENAI_API_KEY configured', response.content.decode())

    def test_reingest_refuses_ready_documents(self):
        document = self.make_document(content=b'word ' * 400)
        ingest_document(document, embedder=FakeEmbedder())
        chunk_ids_before = list(document.chunks.values_list('id', flat=True))

        response = self.client.post(
            reverse('dashboard:reingest-document', args=[document.id]), follow=True
        )

        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.READY)
        self.assertEqual(list(document.chunks.values_list('id', flat=True)), chunk_ids_before)
        self.assertIn('already ready', response.content.decode())

    def test_reingest_of_broken_file_stays_failed_with_reason(self):
        document = self.make_document(content=b'not a pdf at all', filename='broken.pdf')

        response = self.client.post(
            reverse('dashboard:reingest-document', args=[document.id]), follow=True
        )

        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.FAILED)
        self.assertIn('Could not read PDF', document.failure_reason)
        self.assertIn('failed', response.content.decode())

    def test_user_cannot_reingest_another_users_document(self):
        document = self.make_document(user=self.bob)

        response = self.client.post(reverse('dashboard:reingest-document', args=[document.id]))

        self.assertEqual(response.status_code, 404)
        document.refresh_from_db()
        self.assertEqual(document.status, Document.Status.PENDING)
