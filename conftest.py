"""Pytest bootstrap: a controlled environment before Django loads.

Runs before django.setup(), so the settings module is imported with a
deterministic environment -- no dependency on any developer's local
.env file (DJANGO_ENV_FILE points nowhere) and a fixed secret key.
Individual settings-matrix tests re-import the settings module with
their own overrides.
"""

import os
from pathlib import Path

os.environ['DJANGO_ENV_FILE'] = '/nonexistent/.env'
os.environ.pop('DJANGO_DEBUG', None)  # default: False
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'django_agent.settings')
os.environ.setdefault('DJANGO_SECRET_KEY', 'pytest-insecure-secret-key-not-for-production')
os.environ.setdefault('DJANGO_ALLOWED_HOSTS', 'testserver,localhost,127.0.0.1')

# WhiteNoise middleware logs a UserWarning when STATIC_ROOT does not exist;
# tests never run collectstatic, so give it an empty (gitignored) directory.
Path('staticfiles').mkdir(exist_ok=True)

import django  # noqa: E402

django.setup()
