"""Request-scoped logging context shared by middleware and log records."""

import logging
import threading

_local = threading.local()


def set_request_id(request_id: str) -> str:
    _local.request_id = request_id
    return request_id


def clear_request_id() -> None:
    _local.request_id = None


def get_request_id() -> str:
    return getattr(_local, 'request_id', '') or '-'


class RequestIdFilter(logging.Filter):
    """Inject the current request id into every log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True
