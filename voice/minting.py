"""LiveKit access-token minting for the voice surface.

``livekit.api`` is imported lazily inside :func:`mint_voice_token` so the web
process never needs the livekit package to boot: zero-key deployments and
non-voice features are unaffected by the voice integration, its config, or
its dependency state.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.conf import settings

from .flags import voice_identity, voice_room_name

logger = logging.getLogger(__name__)

DEFAULT_TOKEN_TTL_SECONDS = 3600

#: Tokens shorter than this are useless (the holder barely connects before
#: expiry); a bad LIVEKIT_TOKEN_TTL falls back to the default instead.
MIN_TOKEN_TTL_SECONDS = 60


def token_ttl_seconds() -> int:
    """The configured token TTL, bounded below and defaulted on bad input."""
    raw = getattr(settings, 'LIVEKIT_TOKEN_TTL', None) or DEFAULT_TOKEN_TTL_SECONDS
    try:
        ttl = int(raw)
    except (TypeError, ValueError):
        logger.warning('LIVEKIT_TOKEN_TTL is not an integer (%r); using default.', raw)
        ttl = DEFAULT_TOKEN_TTL_SECONDS
    return max(ttl, MIN_TOKEN_TTL_SECONDS)


def mint_voice_token(user: Any) -> dict[str, Any]:
    """Mint a LiveKit token granting join access to the user's own room only.

    The grant is room-scoped (``room_join`` for ``voice-user-<pk>``), so a
    token can only reach the requesting user's voice room -- never another
    user's and never the whole deployment.
    """
    from livekit.api import AccessToken, VideoGrants

    ttl_seconds = token_ttl_seconds()
    room = voice_room_name(user)
    identity = voice_identity(user)
    token = (
        AccessToken(api_key=settings.LIVEKIT_API_KEY, api_secret=settings.LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(getattr(user, 'username', '') or identity)
        .with_ttl(timedelta(seconds=ttl_seconds))
        .with_grants(VideoGrants(room_join=True, room=room))
    )
    logger.info('Minted voice token (user=%s room=%s ttl=%ds).', user.pk, room, ttl_seconds)
    return {
        'token': token.to_jwt(),
        'url': settings.LIVEKIT_URL,
        'room': room,
        'identity': identity,
        'expires_in': ttl_seconds,
    }
