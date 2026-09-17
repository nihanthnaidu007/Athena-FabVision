"""DRF authentication: hashed API keys for programmatic access.

Listed before SessionAuthentication so DRF takes 401 semantics from
this class (rest_framework.views.handle_exception reads
``authenticate_header`` from the first authenticator; without one an
AuthenticationFailed is downgraded to 403).
"""

import hashlib
import hmac

from rest_framework import authentication, exceptions

from .models import ApiKey

# Authorization schemes accepted for API keys, compared case-insensitively.
API_KEY_SCHEMES = ('bearer', 'apikey')


def extract_raw_key(request) -> str | None:
    """Return the presented API key, or None when none is presented.

    Accepted forms: the ``X-API-Key`` header, or
    ``Authorization: Bearer <key>`` / ``Authorization: ApiKey <key>``.
    """
    header_value = request.headers.get('X-API-Key', '')
    if header_value.strip():
        return header_value.strip()
    scheme, _, credential = request.headers.get('Authorization', '').partition(' ')
    if scheme.lower() in API_KEY_SCHEMES and credential.strip():
        return credential.strip()
    return None


class ApiKeyAuthentication(authentication.BaseAuthentication):
    """Authenticate requests presenting an Athena API key.

    Keys are stored only as SHA-256 hashes; the presented key is hashed
    and looked up through the unique index (constant time in the number
    of stored keys), with ``hmac.compare_digest`` as a timing guard.
    Revocation is checked on every request -- nothing about key state is
    cached, so a revoked key fails on its very next use.
    """

    def authenticate(self, request):
        raw_key = extract_raw_key(request)
        if raw_key is None:
            return None  # no key presented; session auth (or anonymous) proceeds
        key_hash = hashlib.sha256(raw_key.encode('utf-8')).hexdigest()
        api_key = (
            ApiKey.objects.select_related('created_by')
            .filter(revoked_at__isnull=True, hashed_key=key_hash)
            .first()
        )
        if api_key is None or not hmac.compare_digest(api_key.hashed_key, key_hash):
            raise exceptions.AuthenticationFailed('Invalid or revoked API key.')
        if not api_key.created_by.is_active:
            raise exceptions.AuthenticationFailed('User account is disabled.')
        return (api_key.created_by, api_key)

    def authenticate_header(self, request):
        return 'ApiKey realm="athena"'
