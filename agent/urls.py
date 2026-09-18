from django.urls import path

from .views import (
    AgentStreamView,
    agent_ask_gone,
    agent_home,
    delete_conversation,
    new_conversation,
    set_conversation_mode,
)

urlpatterns = [
    path('', agent_home, name='agent-home'),  # Redirects to the chat UI
    path('ask/', agent_ask_gone),  # Retired joke shim: permanent 410, meters nothing
    path('stream/', AgentStreamView.as_view(), name='agent-stream'),  # SSE streaming
    path('chat/new/', new_conversation, name='chat-new'),
    path('chat/mode/', set_conversation_mode, name='chat-mode'),
    path('chat/<int:pk>/delete/', delete_conversation, name='chat-delete'),
]
