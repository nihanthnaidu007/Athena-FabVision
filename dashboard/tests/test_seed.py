"""First-run seed tests: exactly two starter documents, once per user, private.

Embeddings come from the deterministic FakeEmbedder -- no test touches
the network or needs an API key.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from assistant.models import Document
from dashboard.seed import (
    EXAMPLE_WAFER_FILENAME,
    WELCOME_FILENAME,
    seed_new_user_knowledge_base,
)
from rag.tests.fakes import FakeEmbedder
from rag.tests.storage import TempMediaMixin

User = get_user_model()


class SeedKnowledgeBaseTests(TempMediaMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('newuser', password='pw12345')

    def documents_for(self, user):
        return Document.objects.for_user(user).order_by('pk')

    def test_seed_creates_exactly_two_documents(self):
        seeded = seed_new_user_knowledge_base(self.user)

        self.assertEqual(len(seeded), 2)
        filenames = {doc.original_filename for doc in seeded}
        self.assertEqual(filenames, {WELCOME_FILENAME, EXAMPLE_WAFER_FILENAME})
        self.assertEqual(self.documents_for(self.user).count(), 2)

    def test_welcome_document_ingests_ready_with_embedder(self):
        embedder = FakeEmbedder()

        seeded = seed_new_user_knowledge_base(self.user, embedder=embedder)

        welcome = next(doc for doc in seeded if doc.original_filename == WELCOME_FILENAME)
        self.assertEqual(welcome.status, 'ready')
        self.assertGreater(welcome.chunks.count(), 0)
        self.assertTrue(all(chunk.embedding for chunk in welcome.chunks.all()))

    def test_welcome_document_is_pending_without_embedder(self):
        # Zero-key deployment: the note is stored and retryable, not
        # silently dropped and not a fake "ready".
        seeded = seed_new_user_knowledge_base(self.user, embedder=None)

        welcome = next(doc for doc in seeded if doc.original_filename == WELCOME_FILENAME)
        welcome.refresh_from_db()
        self.assertEqual(welcome.status, 'pending')
        self.assertEqual(welcome.chunks.count(), 0)

    def test_example_csv_is_storage_only_ready(self):
        seeded = seed_new_user_knowledge_base(self.user)

        csv_document = next(
            doc for doc in seeded if doc.original_filename == EXAMPLE_WAFER_FILENAME
        )
        self.assertEqual(csv_document.file_type, 'csv')
        # Storage-only: usable by the wafer analyzer via path=, no
        # chunking or embedding by design.
        self.assertEqual(csv_document.status, 'ready')
        self.assertEqual(csv_document.chunks.count(), 0)
        with csv_document.file.open('rb') as stored:
            self.assertIn(b'wafer_id,x,y,bin', stored.read())

    def test_duplicate_seed_call_creates_nothing_new(self):
        seed_new_user_knowledge_base(self.user)
        before = [doc.pk for doc in self.documents_for(self.user)]

        seeded_again = seed_new_user_knowledge_base(self.user)

        self.assertEqual(seeded_again, [])
        self.assertEqual([doc.pk for doc in self.documents_for(self.user)], before)

    def test_seeds_are_per_user_and_private(self):
        other_user = User.objects.create_user('otheruser', password='pw12345')

        seeded_for_user = seed_new_user_knowledge_base(self.user)
        seed_new_user_knowledge_base(other_user)

        self.assertEqual(self.documents_for(self.user).count(), 2)
        self.assertEqual(self.documents_for(other_user).count(), 2)
        # No document row is shared across users: each account owns
        # exactly the seeds it was created with.
        self.assertEqual({doc.user_id for doc in seeded_for_user}, {self.user.pk})
        self.assertEqual(
            set(self.documents_for(other_user).values_list('user_id', flat=True)),
            {other_user.pk},
        )
