"""UsageEvent recording tests: one row per authenticated AI call.

Token counts may be zero (no usage data from the current agent); the
row itself is what makes calls countable on the dashboard.
"""

from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from assistant.models import ApiKey, UsageEvent

User = get_user_model()

ASK_URL = '/agent/ask/'


class UsageEventTests(TestCase):
    client_class = APIClient

    def setUp(self):
        self.user = User.objects.create_user('alice')
        self.api_key, self.raw_key = ApiKey.generate(
            created_by=self.user, name='test key'
        )

    def test_usage_event_written_for_key_authenticated_call(self):
        response = self.client.post(
            ASK_URL, {'query': 'hello'}, headers={'X-API-Key': self.raw_key}
        )
        self.assertEqual(response.status_code, 200)
        event = UsageEvent.objects.get()
        self.assertEqual(event.user, self.user)
        self.assertEqual(event.api_key, self.api_key)
        self.assertEqual(event.kind, UsageEvent.Kind.API)
        self.assertEqual(event.tokens_in, 0)
        self.assertEqual(event.tokens_out, 0)
        self.assertGreaterEqual(event.latency_ms, 0)

    def test_usage_event_written_for_session_call_without_api_key(self):
        self.client.force_login(self.user)
        response = self.client.post(ASK_URL, {'query': 'hello'})
        self.assertEqual(response.status_code, 200)
        event = UsageEvent.objects.get()
        self.assertEqual(event.user, self.user)
        self.assertIsNone(event.api_key)

    def test_zero_values_are_recorded_so_calls_are_countable(self):
        self.client.post(ASK_URL, {'query': 'hello'}, headers={'X-API-Key': self.raw_key})
        self.client.post(ASK_URL, {'query': 'again'}, headers={'X-API-Key': self.raw_key})
        self.assertEqual(UsageEvent.objects.count(), 2)
        for event in UsageEvent.objects.all():
            self.assertEqual(event.tokens_in + event.tokens_out, 0)

    def test_recording_failure_does_not_break_the_response(self):
        with mock.patch.object(UsageEvent, 'objects') as manager:
            manager.create.side_effect = RuntimeError('database down')
            response = self.client.post(
                ASK_URL, {'query': 'hello'}, headers={'X-API-Key': self.raw_key}
            )
        self.assertEqual(response.status_code, 200)
        manager.create.assert_called_once()
