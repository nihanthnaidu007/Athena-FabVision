from django.urls import path

from .views import (
    AgentRegenerateView,
    AgentStreamView,
    agent_ask_gone,
    agent_home,
    delete_conversation,
    export_conversation,
    generate_rca_report,
    message_feedback,
    new_conversation,
    rename_conversation,
    set_conversation_mode,
)

urlpatterns = [
    path('', agent_home, name='agent-home'),  # Redirects to the chat UI
    path('ask/', agent_ask_gone),  # Retired joke shim: permanent 410, meters nothing
    path('stream/', AgentStreamView.as_view(), name='agent-stream'),  # SSE streaming
    path('chat/new/', new_conversation, name='chat-new'),
    path('chat/mode/', set_conversation_mode, name='chat-mode'),
    path('chat/rename/', rename_conversation, name='chat-rename'),
    path('chat/<int:pk>/export/', export_conversation, name='chat-export'),
    path('chat/<int:pk>/report/', generate_rca_report, name='chat-rca-report'),
    path('chat/<int:pk>/regenerate/', AgentRegenerateView.as_view(), name='chat-regenerate'),
    path('chat/<int:pk>/delete/', delete_conversation, name='chat-delete'),
    path('feedback/', message_feedback, name='message-feedback'),
]
