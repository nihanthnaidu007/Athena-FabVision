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
OPENAI_CHAT_MODEL = optional_var('OPENAI_CHAT_MODEL', default='gpt-4o-mini')
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
    'assistant',
    'dashboard',
]

MIDDLEWARE = [
    # Outermost: every request gets a correlation id, and any unhandled
    # exception anywhere below becomes the JSON error contract.
    'django_agent.middleware.RequestIdMiddleware',
    'django_agent.middleware.JsonErrorContractMiddleware',
    'django.middleware.security.SecurityMiddleware',
    # Serves collected static files from inside the single-container image
    # (safe with DEBUG=False; collectstatic runs at image build time).
    'whitenoise.middleware.WhiteNoiseMiddleware',
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

# Django REST Framework: every API route requires authentication (session
# for the web app, hashed API keys for programmatic access), and every
# DRF-produced error answers through the JSON error contract.
REST_FRAMEWORK = {
    # ApiKeyAuthentication must stay first: DRF derives 401 semantics
    # (authenticate_header) from the first class in this list.
    'DEFAULT_AUTHENTICATION_CLASSES': [
        'assistant.authentication.ApiKeyAuthentication',
        'rest_framework.authentication.SessionAuthentication',
    ],
    'DEFAULT_PERMISSION_CLASSES': [
        'rest_framework.permissions.IsAuthenticated',
    ],
    'EXCEPTION_HANDLER': 'assistant.exceptions.json_exception_handler',
    # Scoped throttles: session users get the 'user' rate; API-key requests
    # get the rate of their key's tier. Throttled requests answer 429 with
    # Retry-After through the JSON error contract.
    'DEFAULT_THROTTLE_CLASSES': [
        'assistant.throttling.UserRateThrottle',
        'assistant.throttling.ApiKeyTierThrottle',
    ],
    'DEFAULT_THROTTLE_RATES': {
        'user': '120/hour',
        'api_key_standard': '120/hour',
        'api_key_high': '600/hour',
    },
}

# Login route for @login_required pages (the dashboard app serves it
# under /dashboard/accounts/). Must name a mounted route: Django's
# implicit '/accounts/login/' default has no view here and would 404.
LOGIN_URL = optional_var('DJANGO_LOGIN_URL', default='/dashboard/accounts/login/')

# Internationalization

LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files
STATIC_URL = 'static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'

# --- Knowledge-base uploads (rag) --------------------------------------------
# Cap accepted document size. The request-body limit sits slightly above the
# app cap so the rag upload view (not Django's body guard) answers oversize
# requests with the JSON error contract.
RAG_MAX_UPLOAD_BYTES = int_var('RAG_MAX_UPLOAD_BYTES', default=10 * 1024 * 1024)
DATA_UPLOAD_MAX_MEMORY_SIZE = RAG_MAX_UPLOAD_BYTES + 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 1024 * 1024

# Uploaded knowledge-base files are stored under MEDIA_ROOT.
MEDIA_ROOT = BASE_DIR / 'media'
MEDIA_URL = 'media/'

# Security headers and transport settings.
#
# With DEBUG=False the app serves production-safe defaults: HSTS, secure
# cookies, and no-sniff.
#
# W008 disposition (deliberate): SECURE_SSL_REDIRECT stays env-gated and
# defaults to False because the common deployment terminates TLS at an edge
# proxy / load balancer -- the proxy owns the http -> https hop, and an
# in-app redirect there causes redirect loops. It SHOULD be set to true
# (DJANGO_SECURE_SSL_REDIRECT=true) whenever TLS terminates at the app
# itself rather than at an edge proxy, e.g. a directly exposed container
# serving its own certificate.
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
