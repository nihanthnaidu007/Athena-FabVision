"""Health endpoint contract: liveness is process-state only; readiness is
database plus feature flags, key-safe, and honest about degradation."""

import json

import pytest
from django.test import override_settings

from django_agent import health

LIVENESS_PATH = '/healthz/'
READINESS_PATH = '/healthz/ready/'

# Distinctive values so a leak can never pass by coincidence.
SECRET_OPENAI = 'sk-test-openai-do-not-leak-0123456789'
SECRET_LIVEKIT = 'lk-test-livekit-do-not-leak-0123456789'

KEYED_SETTINGS = override_settings(
    OPENAI_API_KEY=SECRET_OPENAI,
    LIVEKIT_URL='wss://example.livekit.cloud',
    LIVEKIT_API_KEY=SECRET_LIVEKIT,
    LIVEKIT_API_SECRET='lk-secret-do-not-leak-0123456789',
)

KEYLESS_SETTINGS = override_settings(
    OPENAI_API_KEY=None,
    LIVEKIT_URL=None,
    LIVEKIT_API_KEY=None,
    LIVEKIT_API_SECRET=None,
)


def test_liveness_is_trivially_ok(client):
    response = client.get(LIVENESS_PATH)

    assert response.status_code == 200
    assert json.loads(response.content) == {'status': 'ok'}


def test_liveness_does_not_touch_the_database(client, monkeypatch):
    monkeypatch.setattr(health, '_database_reachable', lambda: False)

    response = client.get(LIVENESS_PATH)

    assert response.status_code == 200
    assert json.loads(response.content) == {'status': 'ok'}


@pytest.mark.django_db
@KEYLESS_SETTINGS
def test_readiness_keyless_boot_reports_features_disabled(client):
    response = client.get(READINESS_PATH)

    assert response.status_code == 200
    payload = json.loads(response.content)
    assert payload['status'] == 'ok'
    assert payload['checks']['database'] == 'ok'
    assert payload['features'] == {'openai': False, 'livekit': False}


@pytest.mark.django_db
@KEYED_SETTINGS
def test_readiness_keyed_boot_reports_features_available(client):
    response = client.get(READINESS_PATH)

    assert response.status_code == 200
    payload = json.loads(response.content)
    assert payload['features'] == {'openai': True, 'livekit': True}


@pytest.mark.django_db
@KEYED_SETTINGS
def test_readiness_never_leaks_key_material(client):
    response = client.get(READINESS_PATH)

    body = response.content.decode()
    assert SECRET_OPENAI not in body
    assert SECRET_LIVEKIT not in body
    assert 'lk-secret-do-not-leak-0123456789' not in body


@pytest.mark.django_db
def test_readiness_reports_database_outage_as_503(client, monkeypatch):
    monkeypatch.setattr(health, '_database_reachable', lambda: False)

    response = client.get(READINESS_PATH)

    assert response.status_code == 503
    payload = json.loads(response.content)
    assert payload['status'] == 'degraded'
    assert payload['checks']['database'] == 'unreachable'


@pytest.mark.django_db
def test_database_reachable_helper_runs_a_real_probe():
    """The probe itself works against the live (test-database) connection."""
    assert health._database_reachable() is True
