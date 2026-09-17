"""Usage-page tests: auth enforcement, strict isolation, and seeded aggregates."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from assistant.models import UsageEvent

User = get_user_model()


def seed_event(user, *, when, tokens_in=0, tokens_out=0, latency_ms=0):
    """Create a UsageEvent stamped with an explicit created_at."""
    event = UsageEvent.objects.create(
        user=user,
        kind=UsageEvent.Kind.CHAT,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
    )
    # auto_now_add wins on create, so the test sets the timestamp explicitly.
    UsageEvent.objects.filter(pk=event.pk).update(created_at=when)
    return event


class UsagePageAuthTests(TestCase):
    def test_anonymous_get_redirects_to_login(self):
        response = self.client.get(reverse('dashboard:usage'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith('/accounts/login/'))


class UsagePageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user('alice', password='pw12345')
        cls.bob = User.objects.create_user('bob', password='pw12345')
        now = timezone.now()
        # Alice: two recent events with known token/latency values.
        cls.alice_recent = [
            seed_event(
                cls.alice,
                when=now - timedelta(hours=2),
                tokens_in=10,
                tokens_out=20,
                latency_ms=100,
            ),
            seed_event(
                cls.alice,
                when=now - timedelta(hours=1),
                tokens_in=30,
                tokens_out=40,
                latency_ms=300,
            ),
        ]
        # Alice: one event 10 days back (inside 30 days, outside 7).
        cls.alice_old = seed_event(
            cls.alice,
            when=now - timedelta(days=10),
            tokens_in=5,
            tokens_out=5,
            latency_ms=50,
        )
        # Bob: a same-day event that must never surface on Alice's page.
        cls.bob_event = seed_event(
            cls.bob, when=now - timedelta(hours=1), tokens_in=999, tokens_out=999, latency_ms=999
        )

    def setUp(self):
        self.client.force_login(self.alice)

    def test_page_renders_for_owner(self):
        response = self.client.get(reverse('dashboard:usage'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'dashboard/usage.html')

    def test_aggregates_match_seeded_fixtures(self):
        response = self.client.get(reverse('dashboard:usage'), {'days': '30'})
        summary = response.context['summary']
        self.assertEqual(summary['total_calls'], 3)
        self.assertEqual(summary['tokens_in'], 10 + 30 + 5)
        self.assertEqual(summary['tokens_out'], 20 + 40 + 5)
        # latencies 100, 300, 50 -> avg 150; nearest-rank p95 of 3 samples is the max.
        self.assertEqual(summary['avg_latency_ms'], 150)
        self.assertEqual(summary['p95_latency_ms'], 300)
        self.assertEqual(len(summary['daily']), 30)

    def test_other_users_events_are_excluded(self):
        response = self.client.get(reverse('dashboard:usage'))
        summary = response.context['summary']
        self.assertEqual(summary['total_calls'], 2)  # bob's event would make it 3
        self.assertEqual(summary['tokens_in'], 40)  # bob's 999 tokens absent
        self.assertNotIn(str(self.bob_event.tokens_out), response.content.decode())

    def test_days_selector_switches_window(self):
        week = self.client.get(reverse('dashboard:usage'), {'days': '7'})
        self.assertEqual(week.context['summary']['total_calls'], 2)
        month = self.client.get(reverse('dashboard:usage'), {'days': '30'})
        self.assertEqual(month.context['summary']['total_calls'], 3)
        self.assertEqual(month.context['summary']['tokens_in'], 45)

    def test_invalid_days_falls_back_to_default(self):
        response = self.client.get(reverse('dashboard:usage'), {'days': '999'})
        self.assertEqual(response.context['window'], 7)
        self.assertEqual(response.context['summary']['total_calls'], 2)

    def test_empty_state_and_chart_rendering(self):
        empty = User.objects.create_user('carol', password='pw12345')
        self.client.force_login(empty)
        response = self.client.get(reverse('dashboard:usage'))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'No usage recorded in the selected window.')
        self.assertNotContains(response, '<svg class="chart"')
        # With data, the inline SVG bar chart is rendered.
        self.client.force_login(self.alice)
        populated = self.client.get(reverse('dashboard:usage'))
        self.assertContains(populated, '<svg class="chart"')
