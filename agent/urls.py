from django.urls import path
from .views import AgentAPIView, agent_home

urlpatterns = [
    path('', agent_home, name='agent-home'),  # Frontend
    path('ask/', AgentAPIView.as_view(), name='ask-agent'),  # API endpoint
]
