"""Scoped throttle tests: 429 with Retry-After through the JSON contract.

Rates are overridden per test via ``override_settings``; the throttles
read rates live from settings, so overrides apply per request.
"""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from assistant.models import ApiKey

User = get_user_model()

ASK_URL = '/agent/ask/'
CONTRACT_KEYS = {'error', 'code', 'request_id'}


def rates(**overrides):
    """Return a REST_FRAMEWORK override with small test rates."""
    rest_framework = dict(settings.REST_FRAMEWORK)
    rest_framework['DEFAULT_THROTTLE_RATES'] = {
        **rest_framework['DEFAULT_THROTTLE_RATES'],
        **overrides,
    }
    return rest_framework


class ApiKeyTierThrottleTests(TestCase):
    client_class = APIClient

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('alice')
        self.standard_key, self.standard_raw = ApiKey.generate(
            created_by=self.user, name='standard', rate_limit_tier=ApiKey.RateTier.STANDARD
        )
        self.high_key, self.high_raw = ApiKey.generate(
            created_by=self.user, name='high', rate_limit_tier=ApiKey.RateTier.HIGH
        )

    def post_query(self, raw_key):
        return self.client.post(
            ASK_URL, {'query': 'hello'}, headers={'X-API-Key': raw_key}
        )

    @override_settings(REST_FRAMEWORK=rates(
        api_key_standard='2/min', api_key_high='5/min'
    ))
    def test_throttled_key_gets_429_with_retry_after(self):
        self.assertEqual(self.post_query(self.standard_raw).status_code, 200)
        self.assertEqual(self.post_query(self.standard_raw).status_code, 200)
        response = self.post_query(self.standard_raw)
        self.assertEqual(response.status_code, 429)
        payload = response.json()
        self.assertEqual(set(payload), CONTRACT_KEYS)
        self.assertEqual(payload['code'], 'rate_limited')
        self.assertGreaterEqual(int(response['Retry-After']), 1)

    @override_settings(REST_FRAMEWORK=rates(
        api_key_standard='2/min', api_key_high='5/min'
    ))
    def test_high_tier_key_allows_more_requests_than_standard(self):
        for _ in range(5):
            self.assertEqual(self.post_query(self.high_raw).status_code, 200)
        response = self.post_query(self.high_raw)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()['code'], 'rate_limited')

    @override_settings(REST_FRAMEWORK=rates(api_key_standard='1/min'))
    def test_throttle_counters_are_per_key(self):
        self.assertEqual(self.post_query(self.standard_raw).status_code, 200)
        self.assertEqual(self.post_query(self.standard_raw).status_code, 429)
        # A different key in the same tier starts with its own budget.
        _other_key, other_raw = ApiKey.generate(
            created_by=self.user, name='other', rate_limit_tier=ApiKey.RateTier.STANDARD
        )
        self.assertEqual(self.post_query(other_raw).status_code, 200)


class UserRateThrottleTests(TestCase):
    client_class = APIClient

    def setUp(self):
        cache.clear()
        self.user = User.objects.create_user('alice')

    @override_settings(REST_FRAMEWORK=rates(user='2/min'))
    def test_throttled_session_user_gets_429_with_retry_after(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(ASK_URL, {'query': 'q'}).status_code, 200)
        self.assertEqual(self.client.post(ASK_URL, {'query': 'q'}).status_code, 200)
        response = self.client.post(ASK_URL, {'query': 'q'})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()['code'], 'rate_limited')
        self.assertGreaterEqual(int(response['Retry-After']), 1)

    @override_settings(REST_FRAMEWORK=rates(user='2/min'))
    def test_session_throttle_counters_are_per_user(self):
        other = User.objects.create_user('bob')
        self.client.force_login(self.user)
        for _ in range(2):
            self.assertEqual(self.client.post(ASK_URL, {'query': 'q'}).status_code, 200)
        self.assertEqual(self.client.post(ASK_URL, {'query': 'q'}).status_code, 429)
        # Another user starts with a fresh budget.
        self.client.force_login(other)
        self.assertEqual(self.client.post(ASK_URL, {'query': 'q'}).status_code, 200)
