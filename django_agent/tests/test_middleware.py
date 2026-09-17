"""JSON error-contract middleware tests.

These run through Django's real middleware stack (django.test.Client) so
the process_exception wiring behaves exactly as it does in production.
The test-only URLConf (django_agent.tests.urls) provides a raising view
without adding a product endpoint.
"""

import json

import pytest
from django.test import Client, override_settings

from django_agent.tests.urls import raising_view  # noqa: F401  (registers the urlconf)

ERROR_CONTRACT_KEYS = {'error', 'code', 'request_id'}


def test_unhandled_exception_returns_json_error_contract():
    with override_settings(ROOT_URLCONF='django_agent.tests.urls'):
        response = Client().get('/boom/')
    assert response.status_code == 500
    payload = json.loads(response.content)
    assert set(payload) == ERROR_CONTRACT_KEYS
    assert payload['error'] == 'Internal server error'
    assert payload['code'] == 'internal_error'


def test_request_id_flows_into_error_body_and_response_header():
    with override_settings(ROOT_URLCONF='django_agent.tests.urls'):
        response = Client().get('/boom/')
    payload = json.loads(response.content)
    assert response['X-Request-ID'] == payload['request_id']


def test_successful_request_passes_through_with_request_id_header():
    with override_settings(ROOT_URLCONF='django_agent.tests.urls'):
        response = Client().get('/ok/')
    assert response.status_code == 200
    assert response['X-Request-ID']


def test_debug_mode_lets_the_exception_propagate():
    with override_settings(ROOT_URLCONF='django_agent.tests.urls', DEBUG=True):
        with pytest.raises(RuntimeError):
            Client().get('/boom/')


def test_http404_keeps_djangos_standard_404_semantics():
    # Django core renders Http404 as a standard 404; the JSON 500
    # contract must not intercept handled exception types.
    with override_settings(ROOT_URLCONF='django_agent.tests.urls'):
        response = Client().get('/gone/')
    assert response.status_code == 404


def test_request_id_header_still_present_on_handled_404():
    with override_settings(ROOT_URLCONF='django_agent.tests.urls'):
        response = Client().get('/gone/')
    assert response.status_code == 404
    assert response['X-Request-ID']
