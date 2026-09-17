"""UsageEvent recording for authenticated AI calls."""

import logging

from .models import UsageEvent

logger = logging.getLogger(__name__)


def record_usage(
    *,
    user,
    kind,
    api_key=None,
    conversation=None,
    message=None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: int = 0,
    model_name: str = '',
    tool_name: str = '',
):
    """Persist one UsageEvent row for an authenticated AI call.

    Token and latency values may legitimately be zero -- the dashboard
    counts calls with them. A recording failure is logged with full
    detail but never fails the AI response that produced it.
    """
    try:
        return UsageEvent.objects.create(
            user=user,
            kind=kind,
            api_key=api_key,
            conversation=conversation,
            message=message,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            latency_ms=latency_ms,
            model=model_name,
            tool_name=tool_name,
        )
    except Exception:
        logger.exception(
            'Failed to record usage event (user_id=%s kind=%s)',
            getattr(user, 'pk', None),
            kind,
        )
        return None
