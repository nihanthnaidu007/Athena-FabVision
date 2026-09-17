"""
Django settings for Athena FabVision.

All configuration is environment-driven. Required variables raise a
named startup error (django_agent.config.MissingEnvironmentVariable)
instead of an obscure crash deep inside an import; optional integration
keys default to None so the app boots with zero keys into a
degraded-but-alive state. See .env.example for every variable.
"""


from django_agent.config import (
    BASE_DIR,
    bool_var,
    csv_var,
    int_var,
    optional_var,
    required_var,
)

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = required_var('DJANGO_SECRET_KEY')

# Defaults to False (production-safe). Set DJANGO_DEBUG=true only in dev.
DEBUG = bool_var('DJANGO_DEBUG', default=False)

# Comma-separated hostnames; never a wildcard.
ALLOWED_HOSTS = csv_var('DJANGO_ALLOWED_HOSTS', default='localhost,127.0.0.1')

# --- Integration keys -------------------------------------------------------
# All optional: the app must start with none of them configured. Features
# that need a key treat its absence as "feature unavailable", not a crash.
OPENAI_API_KEY = optional_var('OPENAI_API_KEY')
LIVEKIT_URL = optional_var('LIVEKIT_URL')
LIVEKIT_API_KEY = optional_var('LIVEKIT_API_KEY')
LIVEKIT_API_SECRET = optional_var('LIVEKIT_API_SECRET')
GOOGLE_API_KEY = optional_var('GOOGLE_API_KEY')
GOOGLE_SEARCH_ENGINE_ID = optional_var('GOOGLE_SEARCH_ENGINE_ID')

# Application definition

INSTALLED_APPS = [
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'rest_framework',
    'agent',
]

MIDDLEWARE = [
    # Outermost: every request gets a correlation id, and any unhandled
    # exception anywhere below becomes the JSON error contract.
    'django_agent.middleware.RequestIdMiddleware',
    'django_agent.middleware.JsonErrorContractMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'django_agent.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'django_agent.wsgi.application'

# Database
# https://docs.djangoproject.com/en/5.2/ref/settings/#databases

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'db.sqlite3',
    }
}

# Password validation
# https://docs.djangoproject.com/en/5.2/ref/settings/#auth-password-validators

AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# Security headers and transport settings.
#
# With DEBUG=False the app serves production-safe defaults: HSTS, secure
# cookies, and no-sniff. SSL redirect itself defaults to off because the
# common deployment terminates TLS at a proxy (the proxy owns the http ->
# https hop); set DJANGO_SECURE_SSL_REDIRECT=true to enforce it in-app.
if not DEBUG:
    SECURE_SSL_REDIRECT = bool_var('DJANGO_SECURE_SSL_REDIRECT', default=False)
    SECURE_HSTS_SECONDS = int_var('DJANGO_HSTS_SECONDS', default=31536000)
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')
else:
    SECURE_SSL_REDIRECT = False
    SECURE_HSTS_SECONDS = 0
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False

# Logging: structured, single-line console output with request ids.
# Every record carries [rid=...] so requests can be traced end to end.

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'filters': {
        'request_id': {'()': 'django_agent.logging_context.RequestIdFilter'},
    },
    'formatters': {
        'console': {
            'format': '{levelname} {asctime} {name} [rid={request_id}] {message}',
            'style': '{',
        },
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'console',
            'filters': ['request_id'],
        },
    },
    'root': {'handlers': ['console'], 'level': 'INFO'},
    'loggers': {
        'django': {
            'handlers': ['console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}

# Default primary key field type
# https://docs.djangoproject.com/en/5.2/ref/settings/#default-auto-field

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'
