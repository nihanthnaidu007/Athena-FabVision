"""Knowledge-base upload API: authenticated multipart document uploads."""

import hashlib
import logging
from pathlib import Path

from django.conf import settings
from django.core.files.base import ContentFile
from django.http import HttpRequest, JsonResponse
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.views import APIView

from assistant.models import Document
from django_agent.logging_context import get_request_id

from .ingestion import ACCEPTED_EXTENSIONS, STORAGE_ONLY_EXTENSIONS, ingest_document

logger = logging.getLogger(__name__)

DEFAULT_MAX_UPLOAD_BYTES = 10 * 1024 * 1024
EXAMPLE_WAFER_FILENAME = 'wafer_map_example.csv'


def _error_response(request: HttpRequest, status: int, message: str, code: str) -> JsonResponse:
    """The JSON error contract, matching the middleware/DRF shape exactly."""
    request_id = getattr(request, 'request_id', '') or get_request_id()
    return JsonResponse(
        {'error': message, 'code': code, 'request_id': request_id}, status=status
    )


class KbDocumentUploadView(APIView):
    """Upload a .txt/.md/.pdf file into the requesting user's knowledge base.

    Authentication and throttling come from the DRF defaults. The file is
    stored and ingested synchronously; the response reports the honest
    resulting state (``ready``, or ``pending`` in degraded no-key mode).
    Oversize and malformed uploads answer with the JSON error contract.
    """

    parser_classes = [MultiPartParser, FormParser]

    def post(self, request: HttpRequest) -> JsonResponse:
        # APIView parsers read the raw stream, so Django's lazy request.FILES
        # stays empty; MultiPartParser merges uploaded files into request.data.
        upload = request.data.get('file')
        if upload is None:
            return _error_response(
                request,
                400,
                "No file was submitted; send it as the 'file' form field.",
                'validation_error',
            )
        max_bytes = getattr(settings, 'RAG_MAX_UPLOAD_BYTES', DEFAULT_MAX_UPLOAD_BYTES)
        if upload.size > max_bytes:
            return _error_response(
                request,
                413,
                f'File too large ({upload.size} bytes); the limit is {max_bytes} bytes.',
                'payload_too_large',
            )
        suffix = Path(upload.name).suffix.lower()
        if suffix not in ACCEPTED_EXTENSIONS:
            return _error_response(
                request,
                400,
                f'Unsupported file type "{suffix or Path(upload.name).suffix}"; '
                f'accepted: {", ".join(ACCEPTED_EXTENSIONS)}.',
                'validation_error',
            )

        content = upload.read()
        document = Document.objects.create(
            user=request.user,
            original_filename=Path(upload.name).name[:255],
            file_type=suffix.lstrip('.'),
            sha256=hashlib.sha256(content).hexdigest(),
        )
        document.file.save(document.original_filename, ContentFile(content), save=True)

        result = ingest_document(document)
        if result.status == Document.Status.FAILED:
            status = 503 if result.retryable else 400
            code = 'service_unavailable' if result.retryable else 'validation_error'
            return _error_response(request, status, result.detail, code)
        payload = {
            'document_id': document.pk,
            'status': result.status,
            'chunks': result.chunk_count,
            'detail': result.detail,
        }
        if suffix in STORAGE_ONLY_EXTENSIONS:
            # The composer's analyze affordance hands this storage name to
            # the wafer analyzer's existing path= parameter.
            payload['file_path'] = document.file.name
        return JsonResponse(
            payload,
            status=201 if result.status == Document.Status.READY else 202,
        )


def _load_example_wafer_csv() -> bytes | None:
    """The bundled example wafer CSV, or None when it is not installed.

    fabtools is an optional integration (the registry loads it
    defensively), and a deployment may ship without the examples
    directory; both cases degrade to an honest 503, never a crash.
    """
    try:
        import fabtools
    except ImportError:
        return None
    example = Path(fabtools.__file__).parent / 'examples' / EXAMPLE_WAFER_FILENAME
    try:
        return example.read_bytes()
    except OSError:
        return None


class KbExampleWaferView(APIView):
    """One-click ingest of the bundled example wafer CSV into the user's KB.

    Same auth/throttling as the upload view. The example is deduped per
    user on its content hash -- loading twice never duplicates the
    document -- and every user seeds their own copy, so no state is
    shared across users.
    """

    parser_classes = [FormParser]

    def post(self, request: HttpRequest) -> JsonResponse:
        content = _load_example_wafer_csv()
        if content is None:
            return _error_response(
                request,
                503,
                'The example wafer CSV is not installed on this deployment.',
                'service_unavailable',
            )
        sha256 = hashlib.sha256(content).hexdigest()
        existing = Document.objects.for_user(request.user).filter(sha256=sha256).first()
        if existing is not None:
            return JsonResponse(
                {
                    'document_id': existing.pk,
                    'status': existing.status,
                    'chunks': existing.chunks.count(),
                    'detail': 'The example wafer CSV is already in your knowledge base.',
                    'file_path': existing.file.name,
                    'duplicate': True,
                },
                status=200,
            )
        document = Document.objects.create(
            user=request.user,
            original_filename=EXAMPLE_WAFER_FILENAME,
            file_type='csv',
            sha256=sha256,
        )
        document.file.save(EXAMPLE_WAFER_FILENAME, ContentFile(content), save=True)
        result = ingest_document(document)
        if result.status == Document.Status.FAILED:
            return _error_response(request, 503, result.detail, 'service_unavailable')
        return JsonResponse(
            {
                'document_id': document.pk,
                'status': result.status,
                'chunks': result.chunk_count,
                'detail': result.detail,
                'file_path': document.file.name,
            },
            status=201,
        )
