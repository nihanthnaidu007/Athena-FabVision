"""Voice surface views: the page (both states) and the token-mint endpoint."""

from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from rest_framework.response import Response
from rest_framework.views import APIView

from assistant.throttling import ApiKeyTierThrottle, UserRateThrottle
from django_agent.logging_context import get_request_id

from .flags import livekit_configured, voice_identity, voice_room_name
from .minting import mint_voice_token
from .throttling import VoiceTokenThrottle


@login_required
def voice_home(request):
    """The voice page: connect state when configured, setup guide otherwise."""
    if not livekit_configured():
        return render(request, 'voice/setup.html', status=200)
    return render(
        request,
        'voice/index.html',
        {
            'room': voice_room_name(request.user),
            'identity': voice_identity(request.user),
        },
    )


class VoiceTokenView(APIView):
    """POST /voice/token/ -- mint a LiveKit token for the requesting user's room.

    Auth comes from the DRF defaults (session or hashed API key; CSRF is
    enforced for session clients). Throttling is the scoped DRF default
    (per user / per key tier) plus a tighter voice-specific budget on top.
    When LiveKit is not configured the endpoint answers 503 through the
    JSON error contract: the voice surface does not exist on this
    deployment, and nothing else is affected.
    """

    # throttle_classes replaces the DRF defaults, so the default pair is
    # re-listed here to keep session/API-key scoping identical to other routes.
    throttle_classes = [UserRateThrottle, ApiKeyTierThrottle, VoiceTokenThrottle]

    def post(self, request):
        if not livekit_configured():
            return Response(
                {
                    'error': 'Voice is not configured on this deployment.',
                    'code': 'voice_disabled',
                    'request_id': getattr(request, 'request_id', '') or get_request_id(),
                },
                status=503,
            )
        return Response(mint_voice_token(request.user))
