"""Request middleware: request IDs and the JSON error contract."""

import logging
import uuid

from django.conf import settings
from django.http import JsonResponse

from .logging_context import clear_request_id, get_request_id, set_request_id

logger = logging.getLogger(__name__)


class RequestIdMiddleware:
    """Attach a server-generated request id to every request.

    The id is always generated here (client-supplied ids are ignored) so
    log correlation cannot be poisoned. It is set on the request object,
    installed into the thread-local logging context, and echoed back in
    the ``X-Request-ID`` response header.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = uuid.uuid4().hex
        request.request_id = request_id
        set_request_id(request_id)
        try:
            response = self.get_response(request)
        finally:
            clear_request_id()
        response['X-Request-ID'] = request_id
        return response


class JsonErrorContractMiddleware:
    """Convert unhandled view exceptions into the JSON error contract.

    Response body: ``{"error": <message>, "code": <machine code>,
    "request_id": <correlation id>}`` with status 500.

    Active only when ``DEBUG`` is False -- Django's interactive debug
    page must keep working in development. The exception is still logged
    with its request id; it is surfaced, never swallowed.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        return self.get_response(request)

    def process_exception(self, request, exception):
        if settings.DEBUG:
            return None
        request_id = getattr(request, 'request_id', '') or get_request_id()
        logger.exception(
            'Unhandled exception (request_id=%s path=%s)', request_id, request.path
        )
        return JsonResponse(
            {
                'error': 'Internal server error',
                'code': 'internal_error',
                'request_id': request_id,
            },
            status=500,
        )
