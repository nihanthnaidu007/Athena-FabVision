"""Retrieval tests: cosine ranking, contract shape, strict user isolation."""

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.test import TestCase, override_settings

from assistant.models import Chunk, Document
from rag import retrieval
from rag.ingestion import ingest_document
from rag.tests.fakes import FakeEmbedder
from rag.tests.storage import TempMediaMixin

User = get_user_model()

CONTRACT_KEYS = {'document_id', 'chunk_id', 'title', 'snippet', 'score'}


@override_settings(OPENAI_API_KEY=None)
class RetrieveTests(TempMediaMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.alice = User.objects.create_user('alice')
        self.bob = User.objects.create_user('bob')
        self.embedder = FakeEmbedder()

    def ingest_for(self, user, content: bytes, filename: str) -> Document:
        document = Document.objects.create(
            user=user,
            original_filename=filename,
            file_type='txt',
            sha256='0' * 64,
        )
        document.file.save(filename, ContentFile(content), save=True)
        result = ingest_document(document, embedder=self.embedder)
        self.assertEqual(result.status, Document.Status.READY, result.detail)
        return document

    def test_returns_exact_contract_shape(self):
        self.ingest_for(self.alice, b'wafer yield summary', 'lot.txt')

        results = retrieval.retrieve(self.alice, 'wafer', embedder=self.embedder)

        self.assertEqual(len(results), 1)
        self.assertEqual(set(results[0].keys()), CONTRACT_KEYS)
        self.assertIsInstance(results[0]['document_id'], int)
        self.assertIsInstance(results[0]['chunk_id'], int)
        self.assertEqual(results[0]['title'], 'lot.txt')
        self.assertIsInstance(results[0]['score'], float)

    def test_ranking_orders_by_cosine_similarity(self):
        self.ingest_for(self.alice, b'wafer yield summary ' * 20, 'yield-lot.txt')
        # Shares one token ('wafer') with the query: a weaker but positive match.
        self.ingest_for(self.alice, b'wafer etch plasma clean ' * 20, 'etch-recipe.txt')

        results = retrieval.retrieve(
            self.alice, 'wafer yield', k=2, embedder=self.embedder
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]['title'], 'yield-lot.txt')
        self.assertEqual(results[1]['title'], 'etch-recipe.txt')
        self.assertGreater(results[0]['score'], results[1]['score'])
        # The chunk matching the query on every token ranks at the maximum.
        self.assertEqual(results[0]['score'], 1.0)

    def test_k_limits_results(self):
        self.ingest_for(self.alice, b'wafer yield summary ' * 40, 'lot.txt')

        results = retrieval.retrieve(self.alice, 'wafer', k=1, embedder=self.embedder)

        self.assertLessEqual(len(results), 1)

    def test_isolation_user_b_never_retrieves_user_a_documents(self):
        self.ingest_for(
            self.alice, b'wafer yield secret recipe ' * 20, 'alice-secret.txt'
        )
        bob_document = self.ingest_for(self.bob, b'etch plasma notes', 'bob-notes.txt')

        alice_results = retrieval.retrieve(self.alice, 'wafer', embedder=self.embedder)
        bob_results = retrieval.retrieve(self.bob, 'wafer', embedder=self.embedder)

        # Alice only ever sees her own document.
        self.assertTrue(alice_results)
        self.assertEqual(
            {item['title'] for item in alice_results}, {'alice-secret.txt'}
        )
        # Bob's knowledge base holds no wafer content: nothing leaks.
        self.assertEqual(bob_results, [])
        # Every result document id belongs to the requesting user.
        for item in alice_results:
            self.assertIn(item['document_id'], [d.pk for d in self.alice.documents.all()])
        for item in bob_results:
            self.assertEqual(item['document_id'], bob_document.pk)

    def test_chunk_queryset_scoping_is_the_only_access_path(self):
        alice_document = self.ingest_for(
            self.alice, b'wafer yield summary', 'alice.txt'
        )
        bob_document = self.ingest_for(self.bob, b'etch plasma notes', 'bob.txt')

        bob_chunks = Chunk.objects.filter(document__user=self.bob)
        self.assertTrue(bob_chunks.exists())
        # Retrieval joins chunks through document__user, so its queryset can
        # never surface another user's document.
        self.assertEqual(
            set(bob_chunks.values_list('document_id', flat=True)), {bob_document.pk}
        )
        self.assertNotIn(alice_document.pk, set(bob_chunks.values_list('document_id', flat=True)))

    def test_unavailable_without_embedder_returns_empty(self):
        self.ingest_for(self.alice, b'wafer yield summary', 'lot.txt')

        results = retrieval.retrieve(self.alice, 'wafer')  # no key, no injection

        self.assertEqual(results, [])
        self.assertFalse(retrieval.available())

    def test_available_true_when_key_configured(self):
        # Constructing the client performs no network calls.
        with override_settings(OPENAI_API_KEY='test-key'):
            self.assertTrue(retrieval.available())

    def test_snippet_truncates_long_content(self):
        self.ingest_for(self.alice, b'wafer ' + b'x' * 1000, 'long.txt')

        results = retrieval.retrieve(self.alice, 'wafer', embedder=self.embedder)

        self.assertEqual(len(results), 1)
        self.assertLessEqual(len(results[0]['snippet']), retrieval.SNIPPET_LENGTH + 1)
        self.assertTrue(results[0]['snippet'].endswith('…'))

    def test_degenerate_zero_vector_chunks_are_skipped(self):
        # 'unrelated words' has no vocabulary tokens -> zero vector.
        self.ingest_for(self.alice, b'unrelated words only', 'zero.txt')

        results = retrieval.retrieve(self.alice, 'wafer', embedder=self.embedder)

        self.assertEqual(results, [])
