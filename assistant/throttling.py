"""Scoped throttle classes: per-user for session traffic, per-key tier
for API keys.

Rates live in REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'] and are read per
request (not bound at import) so settings overrides apply live. Both
classes subclass SimpleRateThrottle, so a throttled request raises
DRF's Throttled exception and answers 429 + Retry-After through the
JSON error contract.
"""

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from rest_framework import throttling

from .models import ApiKey


def _rate_for_scope(scope: str) -> str:
    """Return the throttle rate string for a scope, read live from settings."""
    rates = settings.REST_FRAMEWORK.get('DEFAULT_THROTTLE_RATES', {})
    if scope not in rates:
        raise ImproperlyConfigured(
            f"No throttle rate configured for scope '{scope}' "
            f"(REST_FRAMEWORK['DEFAULT_THROTTLE_RATES'])."
        )
    return rates[scope]


class UserRateThrottle(throttling.SimpleRateThrottle):
    """Rate-limits session-authenticated users by their user id."""

    scope = 'user'

    def allow_request(self, request, view):
        if isinstance(request.auth, ApiKey):
            return True  # key traffic is scoped by ApiKeyTierThrottle instead
        if not request.user.is_authenticated:
            return True  # unauthenticated requests are rejected before throttling
        return super().allow_request(request, view)

    def get_rate(self) -> str:
        return _rate_for_scope(self.scope)

    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': request.user.pk}


class ApiKeyTierThrottle(throttling.SimpleRateThrottle):
    """Rate-limits API-key requests by the key's rate_limit_tier."""

    scope = 'api_key'  # replaced per request with the tier scope

    def __init__(self):
        # SimpleRateThrottle parses one fixed rate here; ours depends on
        # the request's key tier, so resolution is deferred to allow_request.
        self.rate = None
        self.num_requests = None
        self.duration = None

    def allow_request(self, request, view):
        api_key = request.auth
        if not isinstance(api_key, ApiKey):
            return True  # session traffic is scoped by UserRateThrottle instead
        self.scope = f'api_key_{api_key.rate_limit_tier}'
        self.rate = self.get_rate()
        self.num_requests, self.duration = self.parse_rate(self.rate)
        return super().allow_request(request, view)

    def get_rate(self) -> str:
        return _rate_for_scope(self.scope)

    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': request.auth.pk}
