"""Voice feature flag: the voice surface exists only with a full LiveKit config.

Three settings are required together (URL, key, secret) -- a partial
deployment would mint tokens the worker cannot honor. Everything voice-side
gates on :func:`livekit_configured`; when it is False the rest of the app is
unaffected (zero-key boot stays degraded-but-alive).
"""

from __future__ import annotations

from typing import Any

#: Settings that must all be present for the voice surface to exist.
REQUIRED_LIVEKIT_SETTINGS = ('LIVEKIT_URL', 'LIVEKIT_API_KEY', 'LIVEKIT_API_SECRET')

#: Room-name prefix; the worker parses the user pk back out of it.
VOICE_ROOM_PREFIX = 'voice-user-'


def livekit_configured() -> bool:
    """True when every LiveKit setting is present and non-empty."""
    from django.conf import settings

    return all(getattr(settings, name, None) for name in REQUIRED_LIVEKIT_SETTINGS)


def voice_room_name(user: Any) -> str:
    """The LiveKit room scoped to this user; the worker resolves the pk back."""
    return f'{VOICE_ROOM_PREFIX}{user.pk}'


def voice_identity(user: Any) -> str:
    """Stable LiveKit identity for the user (pk-based: usernames can change)."""
    return f'user-{user.pk}'
