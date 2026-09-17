web: gunicorn --bind 0.0.0.0:$PORT django_agent.wsgi:application
# Voice worker: separate process, only meaningful with LIVEKIT_* configured.
# It never blocks the web service; web boots and serves fine without it.
voice: python -m voice.worker
release: python manage.py migrate --noinput
