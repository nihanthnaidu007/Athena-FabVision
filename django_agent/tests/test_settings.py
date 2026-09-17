"""Settings matrix: env-driven configuration, defaults, and startup errors.

Each test re-imports django_agent.settings with a controlled environment
(DJANGO_ENV_FILE pointed away from any local .env), so assertions run
against real module behavior rather than a cached import.
"""

import importlib
import sys

import pytest
from django.core.exceptions import ImproperlyConfigured

from django_agent.config import InvalidEnvironmentVariable, MissingEnvironmentVariable

INTEGRATION_KEYS = (
    'OPENAI_API_KEY',
    'LIVEKIT_URL',
    'LIVEKIT_API_KEY',
    'LIVEKIT_API_SECRET',
    'GOOGLE_API_KEY',
    'GOOGLE_SEARCH_ENGINE_ID',
)

_MANAGED_VARS = (
    'DJANGO_SECRET_KEY',
    'DJANGO_DEBUG',
    'DJANGO_ALLOWED_HOSTS',
    'DJANGO_SECURE_SSL_REDIRECT',
    'DJANGO_HSTS_SECONDS',
    'DJANGO_ENV_FILE',
    *INTEGRATION_KEYS,
)


def load_settings(monkeypatch, **env):
    """Re-import the settings module under an isolated environment."""
    for name in _MANAGED_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('DJANGO_ENV_FILE', '/nonexistent/.env')
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    for module_name in ('django_agent.settings',):
        sys.modules.pop(module_name, None)
    return importlib.import_module('django_agent.settings')


def test_debug_defaults_false_when_unset(monkeypatch):
    settings = load_settings(monkeypatch, DJANGO_SECRET_KEY='test-secret')
    assert settings.DEBUG is False


def test_debug_reads_env(monkeypatch):
    settings = load_settings(monkeypatch, DJANGO_SECRET_KEY='s', DJANGO_DEBUG='true')
    assert settings.DEBUG is True


def test_invalid_debug_value_is_a_named_error(monkeypatch):
    with pytest.raises(InvalidEnvironmentVariable) as excinfo:
        load_settings(monkeypatch, DJANGO_SECRET_KEY='s', DJANGO_DEBUG='maybe')
    assert 'DJANGO_DEBUG' in str(excinfo.value)


def test_allowed_hosts_reads_env(monkeypatch):
    settings = load_settings(
        monkeypatch,
        DJANGO_SECRET_KEY='s',
        DJANGO_ALLOWED_HOSTS='athena.example.com, foo.example.com',
    )
    assert settings.ALLOWED_HOSTS == ['athena.example.com', 'foo.example.com']


def test_allowed_hosts_default_is_local_only(monkeypatch):
    settings = load_settings(monkeypatch, DJANGO_SECRET_KEY='s')
    assert settings.ALLOWED_HOSTS == ['localhost', '127.0.0.1']


def test_secret_key_reads_env(monkeypatch):
    settings = load_settings(monkeypatch, DJANGO_SECRET_KEY='env-provided-secret')
    assert settings.SECRET_KEY == 'env-provided-secret'


@pytest.mark.parametrize('var', ['DJANGO_SECRET_KEY'])
def test_missing_required_var_raises_named_startup_error(monkeypatch, var):
    with pytest.raises(ImproperlyConfigured) as excinfo:
        load_settings(monkeypatch)  # no DJANGO_SECRET_KEY
    assert var in str(excinfo.value)


def test_missing_required_var_error_is_actionable(monkeypatch):
    with pytest.raises(MissingEnvironmentVariable) as excinfo:
        load_settings(monkeypatch)
    message = str(excinfo.value)
    assert 'STARTUP ERROR' in message
    assert 'DJANGO_SECRET_KEY' in message
    assert '.env.example' in message


def test_app_boots_keyless_into_degraded_state(monkeypatch):
    settings = load_settings(monkeypatch, DJANGO_SECRET_KEY='s')
    for name in INTEGRATION_KEYS:
        assert getattr(settings, name) is None, f'{name} should be optional'


def test_secure_headers_set_when_debug_false(monkeypatch):
    settings = load_settings(monkeypatch, DJANGO_SECRET_KEY='s', DJANGO_DEBUG='false')
    assert settings.SECURE_HSTS_SECONDS > 0
    assert settings.SECURE_HSTS_INCLUDE_SUBDOMAINS is True
    assert settings.SECURE_HSTS_PRELOAD is True
    assert settings.SESSION_COOKIE_SECURE is True
    assert settings.CSRF_COOKIE_SECURE is True


def test_secure_headers_absent_when_debug_true(monkeypatch):
    settings = load_settings(monkeypatch, DJANGO_SECRET_KEY='s', DJANGO_DEBUG='true')
    assert settings.SECURE_HSTS_SECONDS == 0
    assert settings.SESSION_COOKIE_SECURE is False
    assert settings.CSRF_COOKIE_SECURE is False
