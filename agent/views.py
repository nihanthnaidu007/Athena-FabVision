import json
import time
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpRequest, HttpResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from rest_framework import exceptions
from rest_framework.response import Response
from rest_framework.views import APIView

from assistant.models import ApiKey, Conversation, Message, UsageEvent
from assistant.usage import record_usage

from .agent_logic import start_agent
from .llm import default_client
from .loop import run_agent


class AgentAPIView(APIView):
    """
    API endpoint to handle user queries and return agent responses.

    Authentication and permissions come from the DRF defaults (every
    API route requires them); a missing query is raised, not returned,
    so the error body follows the JSON error contract.
    """

    def post(self, request):
        query = request.data.get('query')
        if not query:
            raise exceptions.ValidationError('Query parameter is required')

        started = time.monotonic()
        response = start_agent(query)
        record_usage(
            user=request.user,
            kind=UsageEvent.Kind.API,
            api_key=request.auth if isinstance(request.auth, ApiKey) else None,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        return Response({"response": response})


def agent_home(request):
    """
    Redirect the legacy demo page to the chat UI.
    """
    return redirect('chat-home')


async def agent_sse_stream(**kwargs):
    """Serialize AgentEvents into the text/event-stream wire format."""
    async for event in run_agent(**kwargs):
        payload = json.dumps(event.data, default=str)
        yield f'event: {event.type}\ndata: {payload}\n\n'.encode()


class AgentStreamView(APIView):
    """POST /agent/stream/ -- one agent turn as a Server-Sent Events stream.

    Auth comes from the DRF defaults: session (CSRF enforced by
    SessionAuthentication -- session clients must send the X-CSRFToken
    header rendered in the agent template) or a hashed API key.
    Throttling is the scoped DRF default: per user for sessions, per
    key tier for API keys; a throttled request answers 429 with
    Retry-After through the JSON error contract.

    Body: ``{"message": "...", "conversation_id": <optional pk>}``
    (the legacy ``query`` field is accepted too). The response streams
    ``event: <type>`` / ``data: <json>`` frames for status, delta,
    tool_call, tool_result, sources, done, and error events; failures
    inside the turn are error events, never a broken stream.
    """

    def post(self, request):
        user_input = str(request.data.get('message') or request.data.get('query') or '').strip()
        if not user_input:
            raise exceptions.ValidationError('A non-empty message is required.')

        conversation_id = request.data.get('conversation_id')
        if conversation_id not in (None, ''):
            conversation = get_object_or_404(
                Conversation.objects.for_user(request.user), pk=conversation_id
            )
        else:
            conversation = Conversation.objects.create(
                user=request.user, title=user_input[:200]
            )

        request_id = getattr(request, 'request_id', '')
        response = StreamingHttpResponse(
            agent_sse_stream(
                user=request.user,
                conversation=conversation,
                user_input=user_input,
                request_id=request_id,
                api_key=request.auth if isinstance(request.auth, ApiKey) else None,
            ),
            content_type='text/event-stream',
        )
        response['Cache-Control'] = 'no-cache'
        # Disable proxy buffering (nginx et al.) so deltas arrive live.
        response['X-Accel-Buffering'] = 'no'
        return response


# --- Chat UI -----------------------------------------------------------------
# Server-rendered per the locked frontend decision (no JS build step): the
# page renders the conversation sidebar and the selected thread; chat.js
# hydrates markdown, citation chips, and tool blocks client-side and
# consumes the SSE stream. Every read and mutation goes through the
# user-scoped querysets from assistant.models, so cross-user access 404s.

SIDEBAR_CONVERSATION_LIMIT = 50


def _message_items(messages: list[Message]) -> list[dict[str, Any]]:
    """View-model rows for the thread template.

    Each row carries deterministic ``json_script`` element ids so
    ``chat.js`` can hydrate markdown, citation chips, and tool blocks
    from the persisted JSON fields.
    """
    items: list[dict[str, Any]] = []
    for message in messages:
        pk = str(message.pk)
        items.append(
            {
                'message': message,
                'content_id': f'msg-{pk}-content',
                'sources_id': f'msg-{pk}-sources',
                'blocks_id': f'msg-{pk}-blocks',
            }
        )
    return items


def _conversation_items(user: Any, active_pk: int | None) -> list[dict[str, Any]]:
    """Sidebar rows; ``is_active`` marks the conversation being viewed."""
    conversations = list(Conversation.objects.for_user(user)[:SIDEBAR_CONVERSATION_LIMIT])
    return [
        {'conversation': conversation, 'is_active': conversation.pk == active_pk}
        for conversation in conversations
    ]


def _safe_pk(raw_pk: str | None) -> int | None:
    """Parse a ``?c=`` conversation id; malformed values are "not found"."""
    if raw_pk is None or raw_pk == '':
        return None
    try:
        return int(raw_pk)
    except ValueError:
        raise Http404('No conversation matches the given query.') from None


@login_required
def chat_home(request: HttpRequest) -> HttpResponse:
    """The chat page: conversation sidebar plus the selected message thread."""
    active_pk = _safe_pk(request.GET.get('c'))
    active_conversation = None
    if active_pk is not None:
        active_conversation = get_object_or_404(
            Conversation.objects.for_user(request.user), pk=active_pk
        )
    messages = list(active_conversation.messages.all()) if active_conversation else []
    return render(
        request,
        'agent/chat.html',
        {
            'conversation_items': _conversation_items(request.user, active_pk),
            'active_conversation': active_conversation,
            'message_items': _message_items(messages),
            'llm_configured': default_client() is not None,
        },
    )


@login_required
@require_POST
def new_conversation(request: HttpRequest) -> HttpResponse:
    """Create an empty conversation and open it."""
    title = str(request.POST.get('title') or '').strip()[:200] or 'New conversation'
    conversation = Conversation.objects.create(user=request.user, title=title)
    return redirect(f"{reverse('chat-home')}?c={conversation.pk}")


@login_required
@require_POST
def delete_conversation(request: HttpRequest, pk: int) -> HttpResponse:
    """Delete one of the user's own conversations (messages cascade)."""
    conversation = get_object_or_404(Conversation.objects.for_user(request.user), pk=pk)
    conversation.delete()
    return redirect('chat-home')
