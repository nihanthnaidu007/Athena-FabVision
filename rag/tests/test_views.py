"""Upload API tests: /kb/documents/ auth, contract errors, degraded mode."""

import hashlib
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from assistant.models import ApiKey, Document
from rag.tests.fakes import FakeEmbedder
from rag.tests.storage import TempMediaMixin
from rag.tests.test_ingestion import build_pdf

User = get_user_model()

UPLOAD_URL = '/kb/documents/'


@override_settings(OPENAI_API_KEY=None)
class KbUploadApiTests(TempMediaMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.user = User.objects.create_user('alice', password='fab-password-123')
        self.client = APIClient()
        self.client.login(username='alice', password='fab-password-123')

    def post_file(self, filename: str, content: bytes, **extra):
        return self.client.post(
            UPLOAD_URL,
            {'file': SimpleUploadedFile(filename, content)},
            format='multipart',
            **extra,
        )

    def test_upload_requires_authentication(self):
        self.client.logout()

        response = self.post_file('notes.txt', b'fab notes')

        self.assertEqual(response.status_code, 401)
        self.assertEqual(set(response.json().keys()), {'error', 'code', 'request_id'})
        self.assertEqual(response.json()['code'], 'authentication_required')

    def test_upload_without_file_is_400_contract(self):
        response = self.client.post(UPLOAD_URL, {}, format='multipart')

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(set(body.keys()), {'error', 'code', 'request_id'})
        self.assertEqual(body['code'], 'validation_error')

    def test_upload_unsupported_extension_is_400(self):
        response = self.post_file('lab.exe', b'binary blob')

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['code'], 'validation_error')
        self.assertIn('Unsupported file type', response.json()['error'])

    def test_upload_oversize_is_413_contract(self):
        with override_settings(RAG_MAX_UPLOAD_BYTES=100):
            response = self.post_file('big.txt', b'x' * 200)

        self.assertEqual(response.status_code, 413)
        body = response.json()
        self.assertEqual(set(body.keys()), {'error', 'code', 'request_id'})
        self.assertEqual(body['code'], 'payload_too_large')
        # Nothing was stored for a rejected upload.
        self.assertEqual(Document.objects.count(), 0)

    def test_upload_txt_ready_with_injected_embedder(self):
        embedder = FakeEmbedder()
        content = b'wafer yield summary ' * 40

        with mock.patch('rag.ingestion.default_embedder', return_value=embedder):
            response = self.post_file('lot-notes.txt', content)

        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        self.assertEqual(body['status'], 'ready')
        self.assertGreater(body['chunks'], 0)
        document = Document.objects.get(pk=body['document_id'])
        self.assertEqual(document.status, Document.Status.READY)
        self.assertEqual(document.original_filename, 'lot-notes.txt')
        # The stored file is the uploaded content, and its hash was recorded.
        with document.file.open('rb') as stored:
            self.assertEqual(stored.read(), content)
        self.assertEqual(document.sha256, hashlib.sha256(content).hexdigest())

    def test_upload_pdf_end_to_end(self):
        embedder = FakeEmbedder()
        pdf = build_pdf('wafer yield is nominal this quarter')

        with mock.patch('rag.ingestion.default_embedder', return_value=embedder):
            response = self.post_file('datasheet.pdf', pdf)

        self.assertEqual(response.status_code, 201, response.content)
        document = Document.objects.get(pk=response.json()['document_id'])
        self.assertEqual(document.file_type, 'pdf')
        self.assertEqual(document.status, Document.Status.READY)
        self.assertTrue(
            any(
                'wafer yield is nominal' in chunk.content
                for chunk in document.chunks.all()
            )
        )

    def test_upload_degraded_without_key_is_202_pending(self):
        response = self.post_file('notes.txt', b'wafer yield summary')

        self.assertEqual(response.status_code, 202)
        body = response.json()
        self.assertEqual(body['status'], Document.Status.PENDING)
        self.assertIn('OPENAI_API_KEY', body['detail'])
        document = Document.objects.get(pk=body['document_id'])
        self.assertEqual(document.status, Document.Status.PENDING)
        self.assertEqual(document.chunks.count(), 0)

    def test_upload_malformed_txt_is_400_and_document_failed(self):
        response = self.post_file('broken.txt', b'\xff\xfe not utf-8')

        self.assertEqual(response.status_code, 400)
        body = response.json()
        self.assertEqual(set(body.keys()), {'error', 'code', 'request_id'})
        self.assertEqual(body['code'], 'validation_error')
        document = Document.objects.get(pk=1)
        self.assertEqual(document.status, Document.Status.FAILED)
        self.assertTrue(document.failure_reason)

    def test_upload_api_key_auth_accepted(self):
        _api_key, raw_key = ApiKey.generate(created_by=self.user, name='ci')
        client = APIClient()
        embedder = FakeEmbedder()

        with mock.patch('rag.ingestion.default_embedder', return_value=embedder):
            response = client.post(
                UPLOAD_URL,
                {'file': SimpleUploadedFile('notes.txt', b'wafer yield summary')},
                format='multipart',
                headers={'X-API-Key': raw_key},
            )

        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()['status'], 'ready')

    def test_response_error_carries_request_id_header(self):
        self.client.logout()

        response = self.post_file('notes.txt', b'fab notes')

        header_id = response.headers.get('X-Request-ID')
        self.assertTrue(header_id)
        self.assertEqual(response.json()['request_id'], header_id)
