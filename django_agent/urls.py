from django.contrib import admin
from django.urls import include, path

from agent import views as agent_views
from django_agent import health

urlpatterns = [
    path('admin/', admin.site.urls),
    # The chat page (login required): conversation sidebar + message thread.
    path('', agent_views.chat_home, name='chat-home'),
    path('agent/', include('agent.urls')),
    path('dashboard/', include('dashboard.urls')),
    # Knowledge-base upload API (rag package); consumers gate on import.
    path('kb/', include('rag.urls')),
    # Health: liveness is process-only; readiness probes the database and
    # reports optional-integration availability. Unauthenticated by design.
    path('healthz/', health.liveness, name='liveness'),
    path('healthz/ready/', health.readiness, name='readiness'),
]
