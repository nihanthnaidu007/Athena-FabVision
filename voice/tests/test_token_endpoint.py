"""POST /voice/token/: auth, throttling, room scoping, and the flag-off contract."""

import asyncio
import time

import jwt
import pytest
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import AsyncClient, TestCase, override_settings
from rest_framework.test import APIClient

from assistant.models import ApiKey

from .fakes import LIVEKIT_CONFIG

User = get_user_model()
TOKEN_URL = '/voice/token/'


def throttled_rates(**extra):
    """REST_FRAMEWORK override: base rates raised, voice budget tightened."""
    rates = {**settings.REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'], **extra}
    return {**settings.REST_FRAMEWORK, 'DEFAULT_THROTTLE_RATES': rates}


def mint_with_api_key(user, name='voice test'):
    _key, raw_key = ApiKey.generate(created_by=user, name=name)
    return APIClient().post(TOKEN_URL, headers={'X-API-Key': raw_key})


class VoiceTokenAuthTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='nina', password='pw12345!')
        cache.clear()

    def test_anonymous_is_unauthorized(self):
        response = self.client.post(TOKEN_URL)
        self.assertEqual(response.status_code, 401)

    def test_invalid_api_key_is_unauthorized(self):
        response = self.client.post(TOKEN_URL, headers={'X-API-Key': 'not-a-real-key'})
        self.assertEqual(response.status_code, 401)

    @override_settings(**LIVEKIT_CONFIG)
    def test_api_key_mints_scoped_token(self):
        response = mint_with_api_key(self.user)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload['url'], LIVEKIT_CONFIG['LIVEKIT_URL'])
        self.assertEqual(payload['room'], f'voice-user-{self.user.pk}')
        self.assertEqual(payload['identity'], f'user-{self.user.pk}')
        claims = jwt.decode(
            payload['token'],
            LIVEKIT_CONFIG['LIVEKIT_API_SECRET'],
            algorithms=['HS256'],
            options={'verify_aud': False},
        )
        self.assertEqual(claims['iss'], LIVEKIT_CONFIG['LIVEKIT_API_KEY'])
        grant = claims['video']
        self.assertTrue(grant['roomJoin'])
        self.assertEqual(grant['room'], f'voice-user-{self.user.pk}')
        self.assertEqual(payload['expires_in'], 3600)
        self.assertGreater(claims['exp'], int(time.time()))

    @override_settings(**LIVEKIT_CONFIG)
    def test_room_scoped_to_requesting_user_only(self):
        other = User.objects.create_user(username='lena', password='pw12345!')
        first = mint_with_api_key(self.user, 'first key').json()
        second = mint_with_api_key(other, 'second key').json()
        self.assertNotEqual(first['room'], second['room'])
        self.assertEqual(first['room'], f'voice-user-{self.user.pk}')
        self.assertEqual(second['room'], f'voice-user-{other.pk}')


@override_settings(**{**LIVEKIT_CONFIG, 'LIVEKIT_TOKEN_TTL': 120})
class VoiceTokenTtlTests(TestCase):
    def test_configured_ttl_overrides_default(self):
        user = User.objects.create_user(username='nina', password='pw12345!')
        response = mint_with_api_key(user, 'ttl test')
        self.assertEqual(response.json()['expires_in'], 120)


class VoiceTokenDisabledTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='nina', password='pw12345!')
        self.key, self.raw_key = ApiKey.generate(created_by=self.user, name='disabled test')
        cache.clear()

    @override_settings(**{**LIVEKIT_CONFIG, 'LIVEKIT_URL': ''})
    def test_flag_off_answers_the_json_error_contract(self):
        response = self.client.post(TOKEN_URL, headers={'X-API-Key': self.raw_key})
        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(body['code'], 'voice_disabled')
        self.assertIn('error', body)
        self.assertNotIn('token', body)

    @override_settings(**{**LIVEKIT_CONFIG, 'LIVEKIT_API_KEY': ''})
    def test_partially_configured_counts_as_disabled(self):
        response = self.client.post(TOKEN_URL, headers={'X-API-Key': self.raw_key})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['code'], 'voice_disabled')

    @override_settings(**{**LIVEKIT_CONFIG, 'LIVEKIT_TOKEN_TTL': 'not-a-number'})
    def test_bad_ttl_falls_back_to_default(self):
        response = self.client.post(TOKEN_URL, headers={'X-API-Key': self.raw_key})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['expires_in'], 3600)


@override_settings(**{**LIVEKIT_CONFIG, 'REST_FRAMEWORK': throttled_rates(
    user='1000/hour',
    api_key_standard='1000/hour',
    api_key_high='1000/hour',
    voice_token='1/hour',
)})
class VoiceTokenThrottleTests(TestCase):
    """The voice-specific budget applies on top of the DRF defaults."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(username='nina', password='pw12345!')
        self.key, self.raw_key = ApiKey.generate(created_by=self.user, name='throttle test')
        cache.clear()

    def test_second_mint_within_budget_is_throttled(self):
        first = self.client.post(TOKEN_URL, headers={'X-API-Key': self.raw_key})
        self.assertEqual(first.status_code, 200)
        second = self.client.post(TOKEN_URL, headers={'X-API-Key': self.raw_key})
        self.assertEqual(second.status_code, 429)
        self.assertIn('Retry-After', second.headers)

    def test_throttle_is_per_api_key(self):
        other = User.objects.create_user(username='lena', password='pw12345!')
        first = self.client.post(TOKEN_URL, headers={'X-API-Key': self.raw_key})
        second = mint_with_api_key(other, 'other key')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)


# Session-path tests run as pytest functions with transactional_db: the
# AsyncClient handler thread needs committed session rows, which a TestCase
# write transaction (SQLite) would lock.


@pytest.fixture
def session_user(transactional_db):
    return User.objects.create_user(username='nina', password='x')


def mint_with_session(client, user):
    client.force_login(user)
    page = asyncio.run(client.get('/voice/'))
    csrf = str(page.context['csrf_token'])
    return asyncio.run(client.post(TOKEN_URL, headers={'X-CSRFToken': csrf}))


def test_session_client_with_page_csrf_can_mint(session_user):
    # The page posts its template-meta CSRF token; the session path must
    # work end to end with it.
    with override_settings(**LIVEKIT_CONFIG):
        response = mint_with_session(AsyncClient(), session_user)
    assert response.status_code == 200
    assert 'token' in response.json()


def test_throttle_keyed_by_session_user(session_user):
    with override_settings(
        **{**LIVEKIT_CONFIG, 'REST_FRAMEWORK': throttled_rates(
            user='1000/hour',
            api_key_standard='1000/hour',
            api_key_high='1000/hour',
            voice_token='1/hour',
        )}
    ):
        cache.clear()
        client = AsyncClient()
        first = mint_with_session(client, session_user)
        second = mint_with_session(client, session_user)
    assert first.status_code == 200
    assert second.status_code == 429
