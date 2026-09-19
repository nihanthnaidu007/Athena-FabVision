"""End-to-end wafer CSV pipeline: upload -> storage-only document -> analyzer.

The flagship flow of spec feature 2: a real CSV reaching
``wafer_map_analyze`` through its existing ``path`` parameter, strictly
scoped to the requesting user's own documents. Zero-network: the
analyzer is deterministic. DB-touching tests use a transactional
database so the tool's sync_to_async lookup sees committed rows (the
voice-suite pattern).
"""

import asyncio
import tempfile

import pytest
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from assistant.models import Document
from fabtools import tools
from rag.tests.storage import STATICFILES_BACKEND, STORAGE_BACKEND

User = get_user_model()

UPLOAD_URL = '/kb/documents/'
EXAMPLE_URL = '/kb/documents/example-wafer/'

WAFER_CSV = b'wafer_id,x,y,bin\nW1,0,0,1\nW1,1,0,1\nW1,2,0,3\n'
MALFORMED_CSV = b'id,x,y\nW1,0,0\n'


def run(coro):
    return asyncio.run(coro)


def analyze(user, **kwargs):
    return run(tools.wafer_map_analyze(user, **kwargs))


@pytest.fixture
def temp_media(settings):
    """Throwaway media storage for one test (mirrors rag.tests.storage)."""
    with tempfile.TemporaryDirectory() as media_dir:
        settings.MEDIA_ROOT = media_dir
        settings.STORAGES = {
            'default': {'BACKEND': STORAGE_BACKEND},
            'staticfiles': {'BACKEND': STATICFILES_BACKEND},
        }
        yield media_dir


@pytest.fixture
def user(temp_media, transactional_db):
    return User.objects.create_user('alice', password='fab-password-123')


@pytest.fixture
def client(user):
    client = APIClient()
    client.force_login(user)
    return client


@pytest.fixture
def uploaded_csv(client):
    response = client.post(
        UPLOAD_URL, {'file': SimpleUploadedFile('lot42.csv', WAFER_CSV)}, format='multipart'
    )
    assert response.status_code == 201, response.content
    return response.json()


class TestUploadToAnalyzerPipeline:
    """The flagship flow: a real file reaching a visible wafer analysis."""

    def test_uploaded_csv_analyzes_from_its_storage_name(self, uploaded_csv, user):
        block = analyze(user, path=uploaded_csv['file_path'])

        assert block['type'] == 'wafer_map'
        assert block['total_dies'] == 3
        # The block contract stores yield as a fraction; the UI renders %.
        assert block['yield_pct'] == pytest.approx(2 / 3)

    def test_upload_response_file_path_is_the_storage_name(self, uploaded_csv, user):
        document = Document.objects.get(pk=uploaded_csv['document_id'])

        assert uploaded_csv['file_path'] == document.file.name

    def test_malformed_csv_fails_honestly_at_analysis_not_upload(self, client, user):
        response = client.post(
            UPLOAD_URL,
            {'file': SimpleUploadedFile('broken.csv', MALFORMED_CSV)},
            format='multipart',
        )
        assert response.status_code == 201, response.content
        file_path = response.json()['file_path']

        block = analyze(user, path=file_path)

        assert block['type'] == 'error'
        assert block['code'] == 'wafer_csv_invalid'
        assert 'missing required column' in block['detail']

    def test_example_loader_feeds_the_analyzer(self, client, user):
        response = client.post(EXAMPLE_URL)
        assert response.status_code == 201, response.content

        block = analyze(user, path=response.json()['file_path'])

        assert block['type'] == 'wafer_map'
        assert block['total_dies'] == 81


class TestPipelineIsolation:
    """A storage name resolves only through its owner's documents."""

    def test_other_users_storage_name_is_denied_without_a_read(self, user, uploaded_csv):
        stranger = User.objects.create_user('bob', password='fab-password-123')

        block = analyze(stranger, path=uploaded_csv['file_path'])

        assert block['type'] == 'error'
        assert block['code'] == 'wafer_csv_not_found'
        assert 'knowledge base' in block['detail']

    def test_traversal_attempt_is_denied(self, user):
        block = analyze(user, path='../../../etc/passwd')

        assert block['type'] == 'error'
        assert block['code'] == 'wafer_csv_not_found'

    def test_anonymous_request_is_denied(self, uploaded_csv):
        block = analyze(None, path=uploaded_csv['file_path'])

        assert block['type'] == 'error'
        assert block['code'] == 'wafer_csv_not_found'

    def test_absolute_paths_keep_working_outside_the_kb(self, user, temp_media):
        # The pre-existing contract: an absolute filesystem path reads
        # directly (pinned by fabtools.tests with tmp_path). The KB-name
        # resolution is additive, never a replacement.
        path = f'{temp_media}/direct.csv'
        with open(path, 'wb') as handle:
            handle.write(WAFER_CSV)

        block = analyze(user, path=path)

        assert block['type'] == 'wafer_map'
        assert block['total_dies'] == 3

    def test_deleted_document_no_longer_resolves(self, user, uploaded_csv):
        document = Document.objects.get(pk=uploaded_csv['document_id'])
        document.delete()

        block = analyze(user, path=uploaded_csv['file_path'])

        assert block['type'] == 'error'
        assert block['code'] == 'wafer_csv_not_found'
