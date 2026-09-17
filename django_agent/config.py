"""Fail-fast, named-error environment configuration.

Every runtime variable is read through this module. A missing required
variable raises :class:`MissingEnvironmentVariable` at startup naming the
variable, instead of an obscure crash deep inside an import.

Values are retrieved through python-decouple (Config/RepositoryEnv), so a
repo-root ``.env`` file works exactly as environment variables do. The
env file path can be overridden with ``DJANGO_ENV_FILE`` -- tests use
this to isolate themselves from any local ``.env``.
"""

import os
from pathlib import Path

from decouple import Config, RepositoryEnv, UndefinedValueError
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

ENV_FILE = Path(os.environ.get('DJANGO_ENV_FILE', str(BASE_DIR / '.env')))


# Sentinel returned when a variable is absent; accessors apply their defaults.
_MISSING = object()


class MissingEnvironmentVariable(ImproperlyConfigured):
    """A required environment variable is not set."""

    def __init__(self, name: str):
        self.variable_name = name
        super().__init__(
            f"STARTUP ERROR: required environment variable '{name}' is not set. "
            f"Add it to the environment or to .env (see .env.example)."
        )


class InvalidEnvironmentVariable(ImproperlyConfigured):
    """An environment variable is set but has an unusable value."""

    def __init__(self, name: str, value: str, reason: str):
        self.variable_name = name
        super().__init__(
            f"STARTUP ERROR: environment variable '{name}' has an invalid value "
            f"({reason})."
        )


class _EnvRepository:
    """Decouple repository: values from the env file, then the process env.

    The env file is parsed once at import; process-environment lookups stay
    live so tests can adjust ``os.environ`` between imports.
    """

    def __init__(self, env_file: Path):
        self._file_values = RepositoryEnv(str(env_file)) if env_file.exists() else {}

    def __contains__(self, key: str) -> bool:
        return key in self._file_values or key in os.environ

    def __getitem__(self, key: str) -> str:
        if key in self._file_values:
            return self._file_values[key]
        return os.environ[key]


_decouple = Config(_EnvRepository(ENV_FILE))


def _get(name: str) -> object:
    """Return the raw value, or the _MISSING sentinel when unset."""
    try:
        return _decouple(name)
    except UndefinedValueError:
        return _MISSING


def _is_unset(value: object) -> bool:
    return value is _MISSING or (isinstance(value, str) and value.strip() == '')


def required_var(name: str) -> str:
    """Return the value of a required variable, or raise a named error."""
    value = _get(name)
    if _is_unset(value):
        raise MissingEnvironmentVariable(name)
    return value


def optional_var(name: str, default: str | None = None) -> str | None:
    """Return the value of an optional variable, or the default."""
    value = _get(name)
    if _is_unset(value):
        return default
    return value


def bool_var(name: str, default: bool) -> bool:
    """Parse a boolean variable; an unrecognized value is a named error."""
    value = _get(name)
    if _is_unset(value):
        return default
    normalized = value.strip().lower()
    if normalized in ('true', '1', 'yes', 'on'):
        return True
    if normalized in ('false', '0', 'no', 'off'):
        return False
    raise InvalidEnvironmentVariable(name, value, 'expected true or false')


def csv_var(name: str, default: str) -> list[str]:
    """Parse a comma-separated variable into a cleaned list of strings."""
    value = _get(name)
    if _is_unset(value):
        value = default
    return [item.strip() for item in value.split(',') if item.strip()]


def int_var(name: str, default: int) -> int:
    """Parse an integer variable; a non-integer value is a named error."""
    value = _get(name)
    if _is_unset(value):
        return default
    try:
        return int(value.strip())
    except ValueError:
        raise InvalidEnvironmentVariable(name, value, 'expected an integer') from None
