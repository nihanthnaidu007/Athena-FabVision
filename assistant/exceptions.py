"""DRF exception handling: every DRF-produced error becomes the JSON
error contract (``{"error", "code", "request_id"}``), matching the
shape the middleware produces for unhandled exceptions.

Headers set by DRF's own handler -- ``WWW-Authenticate`` for 401 and
``Retry-After`` for 429 -- survive untouched; only the body is
normalized.
"""

from rest_framework.response import Response
from rest_framework.views import exception_handler as default_exception_handler

from django_agent.logging_context import get_request_id

ERROR_CODES = {
    400: 'validation_error',
    401: 'authentication_required',
    403: 'permission_denied',
    404: 'not_found',
    405: 'method_not_allowed',
    429: 'rate_limited',
}


def _flatten(detail) -> str:
    """Collapse DRF's nested error detail into one readable string."""
    if isinstance(detail, str):
        return detail
    if isinstance(detail, (list, tuple)):
        return '; '.join(_flatten(item) for item in detail)
    if isinstance(detail, dict):
        parts = []
        for key, value in detail.items():
            flattened = _flatten(value)
            parts.append(flattened if key == 'detail' else f'{key}: {flattened}')
        return '; '.join(parts)
    return str(detail)


def json_exception_handler(exc, context) -> Response | None:
    response = default_exception_handler(exc, context)
    if response is None:
        return None  # unhandled exceptions stay with the middleware contract
    request = context.get('request')
    # DRF Request proxies unknown attributes to the underlying HttpRequest,
    # which carries the request id set by RequestIdMiddleware.
    request_id = getattr(request, 'request_id', '') or get_request_id()
    response.data = {
        'error': _flatten(response.data) or 'Request failed.',
        'code': ERROR_CODES.get(response.status_code, 'api_error'),
        'request_id': request_id,
    }
    return response
