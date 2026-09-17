# Athena FabVision -- single-container production image.
#
# Gunicorn serves the exact Procfile command; the container adds a boot
# migration so it is self-sufficient. Platform release phases (the
# Procfile's own `release:` line) remain the path for Heroku-style hosts.
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PORT=8000

WORKDIR /app

# Dependencies first: the lock file changes far less often than the code.
COPY requirements.lock ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.lock

COPY . .

# Static files are collected at build time. Settings require a secret key at
# import; this inline value exists only for the build step and is not baked
# into the image environment (runtime must supply its own).
RUN DJANGO_SECRET_KEY=collectstatic-build-only python manage.py collectstatic --noinput

# Non-root runtime user.
RUN useradd --create-home --uid 1000 athena \
    && chown -R athena:athena /app
USER athena

EXPOSE 8000

# Liveness only: no database dependency, so a DB outage gets the service
# marked unready instead of restarted.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c 'import os, urllib.request; urllib.request.urlopen("http://127.0.0.1:%s/healthz/" % os.environ.get("PORT", "8000"), timeout=3)' || exit 1

# Boot: bring the schema up (idempotent), then serve the Procfile command.
# gunicorn honors WEB_CONCURRENCY for worker scaling.
CMD ["sh", "-c", "python manage.py migrate --noinput && exec gunicorn --bind 0.0.0.0:${PORT} django_agent.wsgi:application"]
