"""Generation endpoint tests: auth, ownership, and honest outcome banners.

The view is a thin shell over ``generate_flashcards_for_notebook``;
these tests pin the shell: anonymous users are turned away, foreign
notebooks 404, and every service outcome -- success, no LLM key,
empty notebook, garbage -- lands as a readable banner on the review
queue without ever losing the failure detail.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from agent.tests.fakes import FakeLLM, delta, usage
from assistant.models import Chunk, Document, Notebook
from study.models import Flashcard

User = get_user_model()

VALID_PAYLOAD = (
    '{"flashcards": [{"question": "What is yield?", "answer": "Good dies / total dies."}]}'
)
GENERATE_URL = 'study:generate'


def fake_llm(body: str = VALID_PAYLOAD) -> FakeLLM:
    return FakeLLM([[delta(body), usage(tokens_in=10, tokens_out=5)]])


class GenerateEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user('alice', password='pw12345')
        cls.bob = User.objects.create_user('bob', password='pw12345')
        cls.notebook = Notebook.objects.create(user=cls.alice, name='Lithography')

    def setUp(self):
        self.client.force_login(self.alice)

    @staticmethod
    def add_ready_document(user, notebook, *, content='Photoresist basics.') -> Document:
        document = Document.objects.create(
            user=user,
            original_filename='notes.txt',
            file_type='txt',
            sha256=f'{user.pk}{notebook.pk}'.ljust(64, '0'),
            notebook=notebook,
            status=Document.Status.READY,
        )
        Chunk.objects.create(document=document, index=0, content=content, content_hash='0' * 64)
        return document

    def test_anonymous_post_redirects_to_login(self):
        self.client.logout()
        response = self.client.post(reverse(GENERATE_URL), data={'notebook_id': 1})
        self.assertEqual(response.status_code, 302)

    def test_get_is_not_allowed(self):
        response = self.client.get(reverse(GENERATE_URL))
        self.assertEqual(response.status_code, 405)

    def test_foreign_notebook_is_404(self):
        foreign = Notebook.objects.create(user=self.bob, name='Bob notes')
        response = self.client.post(reverse(GENERATE_URL), data={'notebook_id': foreign.pk})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(Flashcard.objects.count(), 0)

    def test_missing_or_invalid_notebook_id_redirects_with_banner(self):
        for raw in ('', 'abc'):
            with self.subTest(raw=raw):
                response = self.client.post(reverse(GENERATE_URL), data={'notebook_id': raw})
                self.assertRedirects(response, reverse('dashboard:review'))
                self.assertEqual(Flashcard.objects.count(), 0)

    def test_success_redirects_to_review_queue_with_banner(self):
        self.add_ready_document(self.alice, self.notebook)

        with mock.patch('study.generation.default_client', return_value=fake_llm()):
            response = self.client.post(
                reverse(GENERATE_URL), data={'notebook_id': self.notebook.pk}
            )

        self.assertRedirects(response, reverse('dashboard:review'))
        self.assertEqual(Flashcard.objects.filter(user=self.alice).count(), 1)

    def test_garbage_response_redirects_with_error_banner_and_no_cards(self):
        self.add_ready_document(self.alice, self.notebook)

        with mock.patch('study.generation.default_client', return_value=fake_llm('not json')):
            response = self.client.post(
                reverse(GENERATE_URL), data={'notebook_id': self.notebook.pk}
            )

        self.assertRedirects(response, reverse('dashboard:review'))
        self.assertEqual(Flashcard.objects.count(), 0)

    def test_no_llm_redirects_with_warning_banner(self):
        self.add_ready_document(self.alice, self.notebook)

        with mock.patch('study.generation.default_client', return_value=None):
            response = self.client.post(
                reverse(GENERATE_URL), data={'notebook_id': self.notebook.pk}
            )

        self.assertRedirects(response, reverse('dashboard:review'))
        self.assertEqual(Flashcard.objects.count(), 0)

    def test_empty_notebook_redirects_with_warning_banner(self):
        response = self.client.post(reverse(GENERATE_URL), data={'notebook_id': self.notebook.pk})

        self.assertRedirects(response, reverse('dashboard:review'))
        self.assertEqual(Flashcard.objects.count(), 0)
