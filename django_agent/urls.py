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
    # Practice flashcard generation (v1.1 #6): one budgeted LLM call per
    # POST; the review queue itself lives in the dashboard.
    path('study/', include('study.urls')),
    # Voice surface (feature-flagged): the page renders both states, the
    # token endpoint answers the JSON error contract when unconfigured.
    path('voice/', include('voice.urls')),
    # Knowledge-base upload API (rag package); consumers gate on import.
    path('kb/', include('rag.urls')),
    # Health: liveness is process-only; readiness probes the database and
    # reports optional-integration availability. Unauthenticated by design.
    path('healthz/', health.liveness, name='liveness'),
    path('healthz/ready/', health.readiness, name='readiness'),
]
