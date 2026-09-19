"""Model behavior and cross-user isolation tests.

Isolation contract: user A cannot read, update, or delete user B's
conversations, documents, keys, or usage events through any scoped
accessor -- related managers and ``for_user()`` querysets -- even with
direct queryset calls against a known foreign pk.
"""

import hashlib

from django.contrib.auth import get_user_model
from django.db import IntegrityError
from django.test import TestCase

from assistant.models import (
    ApiKey,
    Chunk,
    Conversation,
    Document,
    Message,
    Notebook,
    UsageEvent,
    hash_raw_key,
)

User = get_user_model()


def make_document(user, **overrides):
    fields = {
        'user': user,
        'original_filename': 'wafer-lot-42.csv',
        'file_type': 'csv',
        'file': 'documents/2026/09/wafer-lot-42.csv',
        'sha256': 'a' * 64,
    }
    fields.update(overrides)
    return Document.objects.create(**fields)


class ApiKeyGenerationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('alice')

    def test_raw_key_is_never_persisted(self):
        api_key, raw_key = ApiKey.generate(created_by=self.user, name='ci')
        self.assertNotEqual(api_key.hashed_key, raw_key)
        self.assertEqual(api_key.hashed_key, hash_raw_key(raw_key))
        self.assertEqual(len(api_key.hashed_key), 64)
        reloaded = ApiKey.objects.get(pk=api_key.pk)
        self.assertNotIn(raw_key, reloaded.hashed_key)

    def test_generate_stores_prefix_for_display(self):
        api_key, raw_key = ApiKey.generate(created_by=self.user, name='ci')
        self.assertEqual(api_key.prefix, raw_key[:8])
        self.assertLessEqual(len(api_key.prefix), 8)

    def test_generate_defaults_to_standard_tier(self):
        api_key, _raw_key = ApiKey.generate(created_by=self.user, name='ci')
        self.assertEqual(api_key.rate_limit_tier, ApiKey.RateTier.STANDARD)

    def test_hash_is_stable_sha256(self):
        self.assertEqual(hash_raw_key('x'), hashlib.sha256(b'x').hexdigest())


class ModelDefaultsTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user('alice')

    def test_conversation_defaults(self):
        conversation = Conversation.objects.create(user=self.user)
        self.assertEqual(conversation.title, 'New conversation')
        self.assertEqual(conversation.mode, Conversation.Mode.ASSISTANT)
        self.assertEqual(
            list(self.user.conversations.all()), [conversation]
        )

    def test_message_defaults(self):
        conversation = Conversation.objects.create(user=self.user)
        message = Message.objects.create(
            conversation=conversation, role=Message.Role.USER, content='hello'
        )
        self.assertEqual(message.sources, [])
        self.assertEqual(message.tool_blocks, [])

    def test_document_defaults(self):
        document = make_document(self.user)
        self.assertEqual(document.status, Document.Status.PENDING)
        self.assertEqual(document.failure_reason, '')

    def test_document_failed_status_carries_reason(self):
        document = make_document(
            self.user,
            status=Document.Status.FAILED,
            failure_reason='unsupported file type',
        )
        self.assertEqual(document.status, Document.Status.FAILED)
        self.assertEqual(document.failure_reason, 'unsupported file type')

    def test_chunk_index_is_unique_per_document(self):
        document = make_document(self.user)
        Chunk.objects.create(
            document=document, index=0, content='a', content_hash='h0'
        )
        with self.assertRaises(IntegrityError):
            Chunk.objects.create(
                document=document, index=0, content='dup', content_hash='h1'
            )


class ConversationIsolationTests(TestCase):
    def setUp(self):
        self.user_a = User.objects.create_user('alice')
        self.user_b = User.objects.create_user('bob')
        self.b_conversation = Conversation.objects.create(user=self.user_b)

    def test_user_a_cannot_read_user_b_conversation(self):
        with self.assertRaises(Conversation.DoesNotExist):
            self.user_a.conversations.get(pk=self.b_conversation.pk)
        self.assertFalse(
            Conversation.objects.for_user(self.user_a)
            .filter(pk=self.b_conversation.pk)
            .exists()
        )

    def test_user_a_cannot_update_user_b_conversation(self):
        updated = (
            Conversation.objects.for_user(self.user_a)
            .filter(pk=self.b_conversation.pk)
            .update(title='hacked')
        )
        self.assertEqual(updated, 0)
        self.b_conversation.refresh_from_db()
        self.assertEqual(self.b_conversation.title, 'New conversation')

    def test_user_a_cannot_delete_user_b_conversation(self):
        deleted, _ = (
            Conversation.objects.for_user(self.user_a)
            .filter(pk=self.b_conversation.pk)
            .delete()
        )
        self.assertEqual(deleted, 0)
        self.assertTrue(
            Conversation.objects.filter(pk=self.b_conversation.pk).exists()
        )

    def test_user_a_conversations_list_excludes_user_b(self):
        Conversation.objects.create(user=self.user_a)
        pks = set(
            Conversation.objects.for_user(self.user_a).values_list('pk', flat=True)
        )
        self.assertEqual(pks, set(self.user_a.conversations.values_list('pk', flat=True)))
        self.assertNotIn(self.b_conversation.pk, pks)


class DocumentIsolationTests(TestCase):
    def setUp(self):
        self.user_a = User.objects.create_user('alice')
        self.user_b = User.objects.create_user('bob')
        self.b_document = make_document(self.user_b)

    def test_user_a_cannot_read_user_b_document(self):
        with self.assertRaises(Document.DoesNotExist):
            self.user_a.documents.get(pk=self.b_document.pk)
        self.assertFalse(
            Document.objects.for_user(self.user_a)
            .filter(pk=self.b_document.pk)
            .exists()
        )

    def test_user_a_cannot_update_user_b_document(self):
        updated = (
            Document.objects.for_user(self.user_a)
            .filter(pk=self.b_document.pk)
            .update(status=Document.Status.FAILED, failure_reason='hacked')
        )
        self.assertEqual(updated, 0)
        self.b_document.refresh_from_db()
        self.assertEqual(self.b_document.status, Document.Status.PENDING)
        self.assertEqual(self.b_document.failure_reason, '')

    def test_user_a_cannot_delete_user_b_document(self):
        deleted, _ = (
            Document.objects.for_user(self.user_a)
            .filter(pk=self.b_document.pk)
            .delete()
        )
        self.assertEqual(deleted, 0)
        self.assertTrue(Document.objects.filter(pk=self.b_document.pk).exists())

    def test_chunks_are_scoped_through_documents(self):
        b_chunk = Chunk.objects.create(
            document=self.b_document, index=0, content='secret', content_hash='h0'
        )
        a_document = make_document(self.user_a)
        self.assertFalse(a_document.chunks.filter(pk=b_chunk.pk).exists())
        self.assertFalse(
            Chunk.objects.filter(document__user=self.user_a, pk=b_chunk.pk).exists()
        )


class ApiKeyIsolationTests(TestCase):
    def setUp(self):
        self.user_a = User.objects.create_user('alice')
        self.user_b = User.objects.create_user('bob')
        self.b_key, _ = ApiKey.generate(created_by=self.user_b, name='bob key')

    def test_user_a_cannot_read_user_b_api_key(self):
        with self.assertRaises(ApiKey.DoesNotExist):
            self.user_a.api_keys.get(pk=self.b_key.pk)
        self.assertFalse(
            ApiKey.objects.for_user(self.user_a).filter(pk=self.b_key.pk).exists()
        )

    def test_user_a_cannot_revoke_user_b_api_key(self):
        updated = (
            ApiKey.objects.for_user(self.user_a)
            .filter(pk=self.b_key.pk)
            .update(revoked_at='2030-01-01T00:00:00Z')
        )
        self.assertEqual(updated, 0)
        self.b_key.refresh_from_db()
        self.assertFalse(self.b_key.is_revoked)

    def test_user_a_cannot_delete_user_b_api_key(self):
        deleted, _ = (
            ApiKey.objects.for_user(self.user_a).filter(pk=self.b_key.pk).delete()
        )
        self.assertEqual(deleted, 0)
        self.assertTrue(ApiKey.objects.filter(pk=self.b_key.pk).exists())


class UsageEventIsolationTests(TestCase):
    def setUp(self):
        self.user_a = User.objects.create_user('alice')
        self.user_b = User.objects.create_user('bob')

    def test_user_a_cannot_read_user_b_usage_events(self):
        b_event = UsageEvent.objects.create(user=self.user_b, kind=UsageEvent.Kind.API)
        self.assertFalse(
            UsageEvent.objects.for_user(self.user_a)
            .filter(pk=b_event.pk)
            .exists()
        )
        self.assertFalse(self.user_a.usage_events.filter(pk=b_event.pk).exists())


class MessageIsolationTests(TestCase):
    def test_user_a_cannot_reach_user_b_messages(self):
        user_a = User.objects.create_user('alice')
        user_b = User.objects.create_user('bob')
        b_conversation = Conversation.objects.create(user=user_b)
        b_message = Message.objects.create(
            conversation=b_conversation, role=Message.Role.USER, content='secret'
        )
        a_conversation = Conversation.objects.create(user=user_a)
        self.assertFalse(a_conversation.messages.filter(pk=b_message.pk).exists())
        self.assertFalse(
            Message.objects.filter(conversation__user=user_a, pk=b_message.pk).exists()
        )


class NotebookModelTests(TestCase):
    """Notebook grouping: per-user uniqueness, isolation, and SET_NULL membership."""

    def setUp(self):
        self.user_a = User.objects.create_user('alice')
        self.user_b = User.objects.create_user('bob')

    def test_name_is_unique_per_user_not_globally(self):
        Notebook.objects.create(user=self.user_a, name='Lithography')
        Notebook.objects.create(user=self.user_b, name='Lithography')  # fine: other user
        with self.assertRaises(IntegrityError):
            Notebook.objects.create(user=self.user_a, name='Lithography')

    def test_isolation_user_a_cannot_read_user_b_notebooks(self):
        b_notebook = Notebook.objects.create(user=self.user_b, name='Bob notes')
        self.assertFalse(Notebook.objects.for_user(self.user_a).filter(pk=b_notebook.pk).exists())
        self.assertFalse(self.user_a.notebooks.filter(pk=b_notebook.pk).exists())

    def test_deleting_notebook_keeps_documents_but_unassigns_them(self):
        notebook = Notebook.objects.create(user=self.user_a, name='Etch')
        document = make_document(self.user_a, notebook=notebook)
        self.assertEqual(document.notebook, notebook)

        notebook.delete()

        document.refresh_from_db()
        self.assertIsNone(document.notebook)  # unassigned, never deleted
        self.assertTrue(Document.objects.filter(pk=document.pk).exists())

    def test_deleting_notebook_keeps_conversations_but_unscopes_them(self):
        notebook = Notebook.objects.create(user=self.user_a, name='Etch')
        conversation = Conversation.objects.create(user=self.user_a, notebook=notebook)
        self.assertEqual(conversation.notebook, notebook)

        notebook.delete()

        conversation.refresh_from_db()
        self.assertIsNone(conversation.notebook)
        self.assertTrue(Conversation.objects.filter(pk=conversation.pk).exists())

    def test_related_managers_group_members(self):
        notebook = Notebook.objects.create(user=self.user_a, name='Lithography')
        document = make_document(self.user_a, notebook=notebook)
        conversation = Conversation.objects.create(user=self.user_a, notebook=notebook)

        self.assertEqual(notebook.documents.count(), 1)
        self.assertEqual(notebook.documents.first(), document)
        self.assertEqual(notebook.conversations.count(), 1)
        self.assertEqual(notebook.conversations.first(), conversation)
