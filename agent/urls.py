from django.urls import path

from .views import (
    AgentAPIView,
    AgentStreamView,
    agent_home,
    delete_conversation,
    new_conversation,
)

urlpatterns = [
    path('', agent_home, name='agent-home'),  # Redirects to the chat UI
    path('ask/', AgentAPIView.as_view(), name='ask-agent'),  # API endpoint
    path('stream/', AgentStreamView.as_view(), name='agent-stream'),  # SSE streaming
    path('chat/new/', new_conversation, name='chat-new'),
    path('chat/<int:pk>/delete/', delete_conversation, name='chat-delete'),
]
