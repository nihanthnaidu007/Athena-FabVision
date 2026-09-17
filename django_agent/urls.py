from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path

from django_agent import health


def home_view(request):
    return HttpResponse(
        '<h1>Welcome to Athena AI Agent</h1>'
        '<p>Go to <a href="/agent/">Agent</a> to interact with the AI.</p>'
    )

urlpatterns = [
    path('admin/', admin.site.urls),
    path('agent/', include('agent.urls')),
    # Health: liveness is process-only; readiness probes the database and
    # reports optional-integration availability. Unauthenticated by design.
    path('healthz/', health.liveness, name='liveness'),
    path('healthz/ready/', health.readiness, name='readiness'),
    path('', home_view, name='home'),
]
