"""API-keys page tests: auth, isolation, and the full create -> use -> revoke lifecycle."""

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from assistant.models import ApiKey, hash_raw_key

User = get_user_model()

# The retired joke endpoint is gone (410); the live API surface these
# key-lifecycle guarantees ride on is the SSE stream endpoint.
STREAM_URL = '/agent/stream/'


class KeysPageAuthTests(TestCase):
    def test_anonymous_list_get_redirects_to_login(self):
        response = self.client.get(reverse('dashboard:keys'))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith(settings.LOGIN_URL))

    def test_anonymous_revoke_post_redirects_to_login(self):
        response = self.client.post(reverse('dashboard:revoke-key', args=[1]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response['Location'].startswith(settings.LOGIN_URL))


class KeysPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user('alice', password='pw12345')
        cls.bob = User.objects.create_user('bob', password='pw12345')

    def setUp(self):
        self.client.force_login(self.alice)

    def create_key(self, name='my key', tier=ApiKey.RateTier.STANDARD):
        """Create a key through the page; returns the one-time raw secret."""
        response = self.client.post(
            reverse('dashboard:keys'), {'name': name, 'rate_limit_tier': tier}
        )
        self.assertEqual(response.status_code, 200)
        return response.context['new_raw_key']

    def test_created_key_is_hashed_and_listed_with_metadata(self):
        raw_key = self.create_key(name='production', tier=ApiKey.RateTier.HIGH)
        stored = ApiKey.objects.get(name='production')
        self.assertEqual(stored.hashed_key, hash_raw_key(raw_key))  # never the raw value
        self.assertEqual(stored.prefix, raw_key[:8])
        self.assertEqual(stored.rate_limit_tier, ApiKey.RateTier.HIGH)
        page = self.client.get(reverse('dashboard:keys')).content.decode()
        self.assertIn('production', page)
        self.assertIn(stored.prefix, page)
        self.assertIn('Active', page)

    def test_full_key_shown_exactly_once(self):
        raw_key = self.create_key()
        # The create response carries the raw secret exactly once...
        self.assertIsNotNone(raw_key)
        # ...a subsequent GET no longer shows it, and only the hash is stored.
        page = self.client.get(reverse('dashboard:keys')).content.decode()
        self.assertNotIn(raw_key, page)
        self.assertIsNone(self.client.get(reverse('dashboard:keys')).context['new_raw_key'])

    def test_created_key_authenticates_api_calls(self):
        raw_key = self.create_key(name='live')
        api_client = APIClient()
        # A 400 means the request passed authentication and reached the
        # view (an invalid or absent key is 401 before the body matters);
        # a missing message body is the cheapest such probe. Usage
        # stamping of keyed requests is pinned end-to-end by
        # agent.tests.test_sse_view's full-turn test.
        response = api_client.post(STREAM_URL, {}, headers={'X-API-Key': raw_key})
        self.assertEqual(response.status_code, 400)

    def test_full_lifecycle_create_use_revoke(self):
        raw_key = self.create_key(name='lifecycle')
        api_client = APIClient()
        probe = api_client.post(STREAM_URL, {}, headers={'X-API-Key': raw_key})
        self.assertEqual(probe.status_code, 400)
        key = ApiKey.objects.get(name='lifecycle')
        self.assertRedirects(
            self.client.post(reverse('dashboard:revoke-key', args=[key.id])),
            reverse('dashboard:keys'),
        )
        # Revocation is checked on every request: the very next use fails.
        revoked = api_client.post(STREAM_URL, {}, headers={'X-API-Key': raw_key})
        self.assertEqual(revoked.status_code, 401)

    def test_revoked_key_shows_revoked_state_without_revoke_button(self):
        raw_key = self.create_key(name='doomed')
        key = ApiKey.objects.get(name='doomed')
        self.client.post(reverse('dashboard:revoke-key', args=[key.id]))
        page = self.client.get(reverse('dashboard:keys')).content.decode()
        self.assertNotIn(raw_key, page)
        self.assertIn('Revoked', page)
        self.assertNotIn('Revoke</button>', page)

    def test_user_cannot_revoke_another_users_key(self):
        self.client.force_login(self.bob)
        self.client.post(
            reverse('dashboard:keys'), {'name': 'bob key', 'rate_limit_tier': 'standard'}
        )
        self.client.force_login(self.alice)
        key_b = ApiKey.objects.get(name='bob key')
        response = self.client.post(reverse('dashboard:revoke-key', args=[key_b.id]))
        self.assertEqual(response.status_code, 404)
        key_b.refresh_from_db()
        self.assertIsNone(key_b.revoked_at)

    def test_keys_page_lists_only_own_keys(self):
        self.create_key(name='alice key')
        self.client.force_login(self.bob)
        self.client.post(
            reverse('dashboard:keys'), {'name': 'bob key', 'rate_limit_tier': 'standard'}
        )
        self.client.force_login(self.alice)
        key_b = ApiKey.objects.get(name='bob key')
        page = self.client.get(reverse('dashboard:keys')).content.decode()
        self.assertIn('alice key', page)
        self.assertNotIn(key_b.prefix, page)
