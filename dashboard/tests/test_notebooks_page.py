"""Notebook manager tests: auth, isolation, CRUD, and document assignment.

The notebooks page is views over the Notebook model's scoped state, so
the interesting behavior mirrors the documents manager: anonymous
requests redirect to login, other users' notebooks 404 on every
mutation, and deleting a notebook unscopes its members instead of
deleting them.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from assistant.models import Conversation, Document, Notebook

User = get_user_model()

NOTEBOOKS_URL = 'dashboard:notebooks'


class NotebooksPageAuthTests(TestCase):
    """Anonymous visitors are redirected to login on every route."""

    def test_anonymous_list_get_redirects_to_login(self):
        response = self.client.get(reverse(NOTEBOOKS_URL))
        self.assertEqual(response.status_code, 302)

    def test_anonymous_create_post_redirects_to_login(self):
        response = self.client.post(reverse(NOTEBOOKS_URL), data={'name': 'X'})
        self.assertEqual(response.status_code, 302)

    def test_anonymous_rename_post_redirects_to_login(self):
        response = self.client.post(reverse('dashboard:rename-notebook', args=[1]))
        self.assertEqual(response.status_code, 302)

    def test_anonymous_delete_post_redirects_to_login(self):
        response = self.client.post(reverse('dashboard:delete-notebook', args=[1]))
        self.assertEqual(response.status_code, 302)

    def test_anonymous_assign_post_redirects_to_login(self):
        response = self.client.post(reverse('dashboard:assign-document-notebook', args=[1]))
        self.assertEqual(response.status_code, 302)


class NotebookManagerPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user('alice', password='pw12345')
        cls.bob = User.objects.create_user('bob', password='pw12345')

    def setUp(self):
        self.client.force_login(self.alice)

    @staticmethod
    def make_document(user, **overrides) -> Document:
        fields = {
            'user': user,
            'original_filename': 'notes.txt',
            'file_type': 'txt',
            'sha256': '0' * 64,
        }
        fields.update(overrides)
        return Document.objects.create(**fields)

    # -- Listing ---------------------------------------------------------

    def test_page_lists_user_notebooks_with_member_counts(self):
        notebook = Notebook.objects.create(user=self.alice, name='Lithography')
        self.make_document(self.alice, notebook=notebook)
        Conversation.objects.create(user=self.alice, notebook=notebook)
        # A second document/conversation pair would double-count without
        # distinct annotations (the cross join), so pin both at 1.
        self.make_document(self.alice, sha256='1' * 64, notebook=notebook)
        Conversation.objects.create(user=self.alice, notebook=notebook, title='second')

        response = self.client.get(reverse(NOTEBOOKS_URL))

        self.assertContains(response, 'Lithography')
        row = next(
            n for n in response.context['notebooks'] if n.pk == notebook.pk
        )
        self.assertEqual(row.document_count, 2)
        self.assertEqual(row.conversation_count, 2)

    def test_page_does_not_list_other_users_notebooks(self):
        Notebook.objects.create(user=self.bob, name='Bob notes')

        response = self.client.get(reverse(NOTEBOOKS_URL))

        self.assertNotContains(response, 'Bob notes')

    # -- Create ----------------------------------------------------------

    def test_create_notebook(self):
        response = self.client.post(reverse(NOTEBOOKS_URL), data={'name': 'Lithography'})

        self.assertRedirects(response, reverse(NOTEBOOKS_URL))
        self.assertTrue(
            Notebook.objects.filter(user=self.alice, name='Lithography').exists()
        )

    def test_create_rejects_blank_name(self):
        self.client.post(reverse(NOTEBOOKS_URL), data={'name': '   '})

        self.assertEqual(Notebook.objects.filter(user=self.alice).count(), 0)

    def test_create_rejects_duplicate_name_for_same_user(self):
        Notebook.objects.create(user=self.alice, name='Lithography')

        response = self.client.post(reverse(NOTEBOOKS_URL), data={'name': 'Lithography'})

        self.assertRedirects(response, reverse(NOTEBOOKS_URL))
        self.assertEqual(Notebook.objects.filter(user=self.alice).count(), 1)

    # -- Rename ----------------------------------------------------------

    def test_rename_notebook(self):
        notebook = Notebook.objects.create(user=self.alice, name='Old name')

        response = self.client.post(
            reverse('dashboard:rename-notebook', args=[notebook.pk]), data={'name': 'New name'}
        )

        self.assertRedirects(response, reverse(NOTEBOOKS_URL))
        notebook.refresh_from_db()
        self.assertEqual(notebook.name, 'New name')

    def test_rename_to_an_existing_sibling_name_is_rejected(self):
        Notebook.objects.create(user=self.alice, name='Other')
        notebook = Notebook.objects.create(user=self.alice, name='Mine')

        self.client.post(
            reverse('dashboard:rename-notebook', args=[notebook.pk]), data={'name': 'Other'}
        )

        notebook.refresh_from_db()
        self.assertEqual(notebook.name, 'Mine')

    def test_rename_foreign_notebook_is_404(self):
        foreign = Notebook.objects.create(user=self.bob, name='Bob notes')

        response = self.client.post(
            reverse('dashboard:rename-notebook', args=[foreign.pk]), data={'name': 'Hacked'}
        )

        self.assertEqual(response.status_code, 404)
        foreign.refresh_from_db()
        self.assertEqual(foreign.name, 'Bob notes')

    # -- Delete ----------------------------------------------------------

    def test_delete_unassigns_documents_and_conversations_but_keeps_them(self):
        notebook = Notebook.objects.create(user=self.alice, name='Etch')
        document = self.make_document(self.alice, notebook=notebook)
        conversation = Conversation.objects.create(user=self.alice, notebook=notebook)

        response = self.client.post(reverse('dashboard:delete-notebook', args=[notebook.pk]))

        self.assertRedirects(response, reverse(NOTEBOOKS_URL))
        self.assertFalse(Notebook.objects.filter(pk=notebook.pk).exists())
        document.refresh_from_db()
        self.assertIsNone(document.notebook)
        conversation.refresh_from_db()
        self.assertIsNone(conversation.notebook)
        self.assertTrue(Document.objects.filter(pk=document.pk).exists())
        self.assertTrue(Conversation.objects.filter(pk=conversation.pk).exists())

    def test_delete_foreign_notebook_is_404(self):
        foreign = Notebook.objects.create(user=self.bob, name='Bob notes')

        response = self.client.post(reverse('dashboard:delete-notebook', args=[foreign.pk]))

        self.assertEqual(response.status_code, 404)
        self.assertTrue(Notebook.objects.filter(pk=foreign.pk).exists())

    # -- Document assignment ----------------------------------------------

    def test_assign_document_to_notebook(self):
        document = self.make_document(self.alice)
        notebook = Notebook.objects.create(user=self.alice, name='Lithography')

        response = self.client.post(
            reverse('dashboard:assign-document-notebook', args=[document.pk]),
            data={'notebook_id': notebook.pk},
        )

        self.assertRedirects(response, reverse('dashboard:documents'))
        document.refresh_from_db()
        self.assertEqual(document.notebook, notebook)

    def test_assign_empty_value_unassigns_document(self):
        notebook = Notebook.objects.create(user=self.alice, name='Lithography')
        document = self.make_document(self.alice, notebook=notebook)

        response = self.client.post(
            reverse('dashboard:assign-document-notebook', args=[document.pk]),
            data={'notebook_id': ''},
        )

        self.assertRedirects(response, reverse('dashboard:documents'))
        document.refresh_from_db()
        self.assertIsNone(document.notebook)

    def test_assign_to_foreign_notebook_is_404(self):
        document = self.make_document(self.alice)
        foreign = Notebook.objects.create(user=self.bob, name='Bob notes')

        response = self.client.post(
            reverse('dashboard:assign-document-notebook', args=[document.pk]),
            data={'notebook_id': foreign.pk},
        )

        self.assertEqual(response.status_code, 404)
        document.refresh_from_db()
        self.assertIsNone(document.notebook)

    def test_assign_foreign_document_is_404(self):
        bob_document = self.make_document(self.bob)
        notebook = Notebook.objects.create(user=self.alice, name='Lithography')

        response = self.client.post(
            reverse('dashboard:assign-document-notebook', args=[bob_document.pk]),
            data={'notebook_id': notebook.pk},
        )

        self.assertEqual(response.status_code, 404)
        bob_document.refresh_from_db()
        self.assertIsNone(bob_document.notebook)

    def test_documents_page_renders_assignment_selector_with_user_notebooks(self):
        document = self.make_document(self.alice)
        Notebook.objects.create(user=self.alice, name='Lithography')
        Notebook.objects.create(user=self.bob, name='Bob notes')

        response = self.client.get(reverse('dashboard:documents'))

        html = response.content.decode()
        self.assertIn(reverse('dashboard:assign-document-notebook', args=[document.pk]), html)
        self.assertIn('Lithography', html)
        self.assertNotIn('Bob notes', html)
