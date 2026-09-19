"""Review queue page tests (v1.1 #6): ordering, isolation, and grading.

The queue is a read over the caller's flashcards ordered by due date;
grading reuses the SM-2 scheduler, so these tests pin the page
behavior -- due cards first and oldest due at the top, foreign cards
unreachable, offline operation, and the generate affordance tracking
the user's notebooks.
"""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from assistant.models import Notebook
from study.models import Flashcard, ReviewLog

User = get_user_model()
REVIEW_URL = 'dashboard:review'


def make_card(user, notebook=None, *, question='Q?', due_at=None, **kwargs) -> Flashcard:
    return Flashcard.objects.create(
        user=user,
        notebook=notebook,
        question=question,
        answer='A.',
        due_at=due_at or timezone.now(),
        **kwargs,
    )


class ReviewQueueTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user('alice', password='pw12345')
        cls.bob = User.objects.create_user('bob', password='pw12345')
        cls.notebook = Notebook.objects.create(user=cls.alice, name='Lithography')

    def setUp(self):
        self.client.force_login(self.alice)

    def test_requires_login(self):
        self.client.logout()
        response = self.client.get(reverse(REVIEW_URL))
        self.assertEqual(response.status_code, 302)

    def test_empty_queue_with_notebooks_offers_generation(self):
        response = self.client.get(reverse(REVIEW_URL))
        self.assertContains(response, 'Nothing due')
        self.assertContains(response, '/study/generate/')

    def test_empty_queue_without_notebooks_points_to_notebooks(self):
        Notebook.objects.all().delete()
        response = self.client.get(reverse(REVIEW_URL))
        self.assertContains(response, 'Nothing due')
        self.assertContains(response, 'No notebooks yet')

    def test_due_cards_ordered_oldest_due_first(self):
        now = timezone.now()
        make_card(self.alice, self.notebook, question='due second', due_at=now - timedelta(hours=1))
        make_card(self.alice, self.notebook, question='due first', due_at=now - timedelta(hours=2))

        response = self.client.get(reverse(REVIEW_URL))

        body = response.content.decode()
        first, second = body.find('due first'), body.find('due second')
        self.assertLess(first, second)

    def test_foreign_cards_never_appear(self):
        make_card(self.bob, None, question='Bob private card')

        response = self.client.get(reverse(REVIEW_URL))

        self.assertNotContains(response, 'Bob private card')
        self.assertContains(response, 'Nothing due')

    def test_generate_form_lists_only_own_notebooks(self):
        Notebook.objects.create(user=self.bob, name='Bob notebook')

        response = self.client.get(reverse(REVIEW_URL))

        self.assertContains(response, '/study/generate/')
        self.assertContains(response, 'Lithography')
        self.assertNotContains(response, 'Bob notebook')

    def test_upcoming_count_is_surfaced(self):
        now = timezone.now()
        make_card(self.alice, self.notebook, due_at=now + timedelta(days=3))
        response = self.client.get(reverse(REVIEW_URL))
        self.assertContains(response, '1 card scheduled for later')

    def test_upcoming_cards_are_not_due(self):
        now = timezone.now()
        make_card(self.alice, self.notebook, question='future card', due_at=now + timedelta(days=3))
        response = self.client.get(reverse(REVIEW_URL))
        self.assertNotContains(response, 'future card')


class GradeFlashcardTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user('alice', password='pw12345')
        cls.bob = User.objects.create_user('bob', password='pw12345')

    def setUp(self):
        self.client.force_login(self.alice)

    def test_grade_redirects_back_to_the_queue(self):
        card = make_card(self.alice)
        response = self.client.post(
            reverse('dashboard:grade-flashcard', args=[card.pk]), data={'grade': '5'}
        )
        self.assertRedirects(response, reverse(REVIEW_URL))

    def test_grade_schedules_the_next_due_date_by_sm2(self):
        card = make_card(self.alice)
        self.client.post(reverse('dashboard:grade-flashcard', args=[card.pk]), data={'grade': '5'})
        card.refresh_from_db()
        self.assertEqual(card.repetitions, 1)
        self.assertEqual(card.interval_days, 1)
        self.assertGreater(card.due_at, timezone.now())

    def test_grade_writes_a_review_log_row(self):
        card = make_card(self.alice)
        self.client.post(reverse('dashboard:grade-flashcard', args=[card.pk]), data={'grade': '3'})
        self.assertEqual(ReviewLog.objects.filter(user=self.alice, card=card, grade=3).count(), 1)

    def test_foreign_card_is_404(self):
        foreign = make_card(self.bob)
        response = self.client.post(
            reverse('dashboard:grade-flashcard', args=[foreign.pk]), data={'grade': '5'}
        )
        self.assertEqual(response.status_code, 404)

    def test_invalid_grade_redirects_with_banner_and_no_change(self):
        card = make_card(self.alice)
        before = card.due_at
        for raw in ('', 'nine', '6'):
            with self.subTest(raw=raw):
                response = self.client.post(
                    reverse('dashboard:grade-flashcard', args=[card.pk]), data={'grade': raw}
                )
                self.assertRedirects(response, reverse(REVIEW_URL))
        card.refresh_from_db()
        self.assertEqual(card.due_at, before)
        self.assertEqual(card.repetitions, 0)

    def test_get_is_not_allowed(self):
        card = make_card(self.alice)
        response = self.client.get(reverse('dashboard:grade-flashcard', args=[card.pk]))
        self.assertEqual(response.status_code, 405)
