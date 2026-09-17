"""Health endpoints for orchestrators, load balancers, and uptime probes.

Two semaphores, not one:

- ``/healthz/`` (liveness): answers from process state alone. If this
  fails, the process itself is broken and should be restarted.
- ``/healthz/ready/`` (readiness): verifies the database is reachable and
  reports which optional integrations are configured. Answers 503 when a
  dependency is down so traffic is routed away instead of the container
  being restarted.

Both work with zero keys configured (degraded-but-alive boot) and expose
only booleans -- never key material.
"""

import logging

from django.conf import settings
from django.db import connection
from django.http import JsonResponse

logger = logging.getLogger(__name__)


def _database_reachable() -> bool:
    """Probe the default connection with SELECT 1."""
    try:
        with connection.cursor() as cursor:
            cursor.execute('SELECT 1')
        return True
    except Exception:
        # A health probe converts a broken dependency into a status code;
        # the exception is logged so the failure is never silent.
        logger.warning('Readiness check: database is unreachable', exc_info=True)
        return False


def _livekit_configured() -> bool:
    """Voice needs the room URL and both API credentials to function."""
    return bool(
        settings.LIVEKIT_URL
        and settings.LIVEKIT_API_KEY
        and settings.LIVEKIT_API_SECRET
    )


def liveness(request):
    """Trivially OK: the process is up and able to answer."""
    return JsonResponse({'status': 'ok'})


def readiness(request):
    """Database reachability plus which optional integrations are configured."""
    database_ok = _database_reachable()
    payload = {
        'status': 'ok' if database_ok else 'degraded',
        'checks': {
            'database': 'ok' if database_ok else 'unreachable',
        },
        'features': {
            'openai': bool(settings.OPENAI_API_KEY),
            'livekit': _livekit_configured(),
        },
    }
    return JsonResponse(payload, status=200 if database_ok else 503)
