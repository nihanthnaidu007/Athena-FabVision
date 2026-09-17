"""Token-minting throttle: one shared scope for sessions and API keys.

The DRF defaults already throttle per user (sessions) or per key tier; this
class adds a tighter, voice-specific budget on top, keyed by whichever
identity authenticated the request.
"""

from rest_framework import throttling

from assistant.models import ApiKey
from assistant.throttling import _rate_for_scope


class VoiceTokenThrottle(throttling.SimpleRateThrottle):
    """Rate-limits token minting per user (sessions) or per key (API keys)."""

    scope = 'voice_token'

    def get_rate(self) -> str:
        # Same live-settings contract as the assistant throttles: rates are
        # read per request so overrides apply without a restart.
        return _rate_for_scope(self.scope)

    def get_cache_key(self, request, view):
        if isinstance(request.auth, ApiKey):
            ident = f'key-{request.auth.pk}'
        elif request.user.is_authenticated:
            ident = f'user-{request.user.pk}'
        else:
            return None  # unauthenticated requests are rejected before throttling
        return self.cache_format % {'scope': self.scope, 'ident': ident}
