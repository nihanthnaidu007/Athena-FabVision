web: gunicorn --bind 0.0.0.0:$PORT django_agent.wsgi:application
release: python manage.py migrate --noinput
