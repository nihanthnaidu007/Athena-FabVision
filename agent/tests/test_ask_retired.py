"""The retired /agent/ask/ joke shim answers 410 Gone and meters nothing.

Spec #12 retired the canned-joke endpoint: old integrators get an
explicit, explained Gone instead of a fake answer, and the usage
dashboard stops recording events for responses no model produced.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase

from assistant.models import ApiKey, UsageEvent

User = get_user_model()

ASK_URL = '/agent/ask/'


class RetiredAskEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user('alice', password='pw12345')

    def test_post_answers_410_with_error_contract(self):
        response = self.client.post(
            ASK_URL, {'query': 'tell me a joke'}, content_type='application/json'
        )
        self.assertEqual(response.status_code, 410)
        payload = response.json()
        self.assertEqual(payload['code'], 'endpoint_retired')
        self.assertIn('/agent/stream/', payload['error'])
        self.assertTrue(payload['request_id'])

    def test_get_is_gone_too(self):
        # The old DRF view answered 405 for GET; a plain Gone for every
        # method keeps the retirement story uniform for old clients.
        response = self.client.get(ASK_URL)
        self.assertEqual(response.status_code, 410)
        self.assertEqual(response.json()['code'], 'endpoint_retired')

    def test_valid_api_key_gets_gone_and_meters_nothing(self):
        _api_key, raw = ApiKey.generate(name='k', created_by=self.user)
        response = self.client.post(ASK_URL, HTTP_X_API_KEY=raw)
        self.assertEqual(response.status_code, 410)
        # The old shim recorded a real API UsageEvent per joke; the 410
        # must not pollute the usage dashboard with fake traffic.
        self.assertEqual(UsageEvent.objects.count(), 0)
