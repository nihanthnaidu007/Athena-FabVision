"""API-key authentication tests, run through the real DRF stack.

Covers the acceptance paths: unauthenticated requests get 401/403 in
the JSON error contract, invalid and revoked keys get 401 on their
next use (revocation is never cached), and both header forms work.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from assistant.models import ApiKey

User = get_user_model()

ASK_URL = '/agent/ask/'
CONTRACT_KEYS = {'error', 'code', 'request_id'}


class ApiKeyAuthenticationTests(TestCase):
    client_class = APIClient

    def setUp(self):
        self.user = User.objects.create_user('alice')
        self.api_key, self.raw_key = ApiKey.generate(
            created_by=self.user, name='test key'
        )

    def post_query(self, **headers):
        return self.client.post(ASK_URL, {'query': 'hello'}, headers=headers)

    def test_valid_key_authenticates_via_x_api_key(self):
        response = self.post_query(**{'X-API-Key': self.raw_key})
        self.assertEqual(response.status_code, 200)
        self.assertIn('response', response.json())

    def test_valid_key_authenticates_via_bearer_scheme(self):
        response = self.post_query(Authorization=f'Bearer {self.raw_key}')
        self.assertEqual(response.status_code, 200)

    def test_valid_key_authenticates_via_apikey_scheme(self):
        response = self.post_query(Authorization=f'ApiKey {self.raw_key}')
        self.assertEqual(response.status_code, 200)

    def test_unauthenticated_request_gets_401_with_json_contract(self):
        # DRF raises NotAuthenticated (401, not 403) when authenticators are
        # configured but none succeeded; the first authenticator's
        # authenticate_header drives the WWW-Authenticate challenge.
        response = self.post_query()
        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(set(payload), CONTRACT_KEYS)
        self.assertEqual(payload['code'], 'authentication_required')
        self.assertEqual(payload['request_id'], response['X-Request-ID'])
        self.assertEqual(response['WWW-Authenticate'], 'ApiKey realm="athena"')

    def test_get_is_rejected_for_unauthenticated_requests_too(self):
        response = self.client.get(ASK_URL)
        self.assertIn(response.status_code, (401, 403))

    def test_invalid_key_gets_401_with_json_contract(self):
        response = self.post_query(**{'X-API-Key': 'not-a-real-key'})
        self.assertEqual(response.status_code, 401)
        payload = response.json()
        self.assertEqual(set(payload), CONTRACT_KEYS)
        self.assertEqual(payload['code'], 'authentication_required')

    def test_malformed_authorization_header_gets_401_challenge(self):
        # An unrecognized scheme presents no usable credentials, so the
        # request is treated as unauthenticated with the 401 challenge.
        response = self.post_query(Authorization='Basic dXNlcjpwYXNz')
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['code'], 'authentication_required')

    def test_revoked_key_is_rejected_immediately(self):
        self.assertEqual(self.post_query(**{'X-API-Key': self.raw_key}).status_code, 200)
        self.api_key.revoked_at = self.api_key.created_at
        self.api_key.save()
        response = self.post_query(**{'X-API-Key': self.raw_key})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()['code'], 'authentication_required')

    def test_key_of_disabled_user_is_rejected(self):
        self.user.is_active = False
        self.user.save()
        response = self.post_query(**{'X-API-Key': self.raw_key})
        self.assertEqual(response.status_code, 401)

    def test_missing_query_gets_validation_error_contract(self):
        response = self.client.post(ASK_URL, {}, headers={'X-API-Key': self.raw_key})
        self.assertEqual(response.status_code, 400)
        payload = response.json()
        self.assertEqual(set(payload), CONTRACT_KEYS)
        self.assertEqual(payload['code'], 'validation_error')
        self.assertIn('Query parameter is required', payload['error'])
