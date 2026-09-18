import json
from typing import Any

from django.contrib.auth.decorators import login_required
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from rest_framework import exceptions
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.renderers import JSONRenderer
from rest_framework.views import APIView

from assistant.models import ApiKey, Conversation, Message, MessageFeedback
from django_agent.logging_context import get_request_id

from .llm import default_client
from .loop import run_agent


def agent_ask_gone(request):
    """410 for the retired /agent/ask/ joke shim (spec #12 hygiene).

    The legacy endpoint answered canned keyword-routed jokes without the
    model and metered them as real API usage. Old integrators now get an
    explicit, explained Gone -- pointing at /agent/stream/ -- instead of
    a misleading 404 or a fake answer, and no UsageEvent is recorded.
    """
    request_id = getattr(request, 'request_id', '') or get_request_id()
    return JsonResponse(
        {
            'error': 'This endpoint was retired; use POST /agent/stream/ for agent turns.',
            'code': 'endpoint_retired',
            'request_id': request_id,
        },
        status=410,
    )


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


class EventStreamNegotiation(DefaultContentNegotiation):
    """Accept the stream's own media type instead of the renderer dance.

    The default negotiation 406s any ``Accept`` it cannot match to a
    renderer -- including ``text/event-stream``, which this endpoint
    exists to produce (EventSource and curl send it by default). Parser
    selection stays default so request bodies keep negotiating normally;
    error responses render through JSONRenderer, keeping the JSON error
    contract.
    """

    def select_renderer(self, request, renderers, format_suffix):
        return renderers[0], renderers[0].format


class AgentStreamView(APIView):
    """POST /agent/stream/ -- one agent turn as a Server-Sent Events stream.

    Auth comes from the DRF defaults: session (CSRF enforced by
    SessionAuthentication -- session clients must send the X-CSRFToken
    header rendered in the agent template) or a hashed API key.
    Throttling is the scoped DRF default: per user for sessions, per
    key tier for API keys; a throttled request answers 429 with
    Retry-After through the JSON error contract.

    Body: ``{"message": "...", "conversation_id": <optional pk>}``
    (the legacy ``query`` field is accepted too). ``mode`` (optional,
    ``assistant``|``tutor``) sets a newly created conversation's mode and
    is honored only at creation -- an existing conversation's persisted
    mode always rules; the chat-mode endpoint changes it. The response
    streams ``event: <type>`` / ``data: <json>`` frames for status, delta,
    tool_call, tool_result, sources, done, and error events; failures
    inside the turn are error events, never a broken stream.
    """

    renderer_classes = [JSONRenderer]
    content_negotiation_class = EventStreamNegotiation

    def post(self, request):
        user_input = str(request.data.get('message') or request.data.get('query') or '').strip()
        if not user_input:
            raise exceptions.ValidationError('A non-empty message is required.')

        mode = str(request.data.get('mode') or '').strip()
        if mode and mode not in Conversation.Mode.values:
            raise exceptions.ValidationError('Unknown mode.')

        conversation_id = request.data.get('conversation_id')
        if conversation_id not in (None, ''):
            conversation = get_object_or_404(
                Conversation.objects.for_user(request.user), pk=conversation_id
            )
        else:
            conversation = Conversation.objects.create(
                user=request.user,
                title=user_input[:200],
                mode=mode or Conversation.Mode.ASSISTANT,
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
def set_conversation_mode(request: HttpRequest) -> HttpResponse:
    """Set the tutor toggle for one of the user's own conversations.

    Body: ``conversation_id`` plus ``mode`` (``assistant`` or ``tutor``);
    answers JSON with the persisted mode. Foreign conversations 404 and
    unknown modes 400 -- both are surfaced by the client, never silent.
    """
    conversation = get_object_or_404(
        Conversation.objects.for_user(request.user),
        pk=_safe_pk(request.POST.get('conversation_id')),
    )
    mode = str(request.POST.get('mode') or '').strip()
    if mode not in Conversation.Mode.values:
        return HttpResponseBadRequest('Unknown mode.')
    conversation.mode = mode
    conversation.save(update_fields=['mode', 'updated_at'])
    return JsonResponse({'conversation_id': conversation.pk, 'mode': mode})


@login_required
@require_POST
def delete_conversation(request: HttpRequest, pk: int) -> HttpResponse:
    """Delete one of the user's own conversations (messages cascade)."""
    conversation = get_object_or_404(Conversation.objects.for_user(request.user), pk=pk)
    conversation.delete()
    return redirect('chat-home')


@login_required
@require_POST
def message_feedback(request: HttpRequest) -> HttpResponse:
    """Record, change, or clear the user's feedback on one assistant message.

    Body: ``message_id`` plus ``value`` (``up`` | ``down`` | ``none``).
    ``up``/``down`` upsert the one feedback row per user+message;
    ``none`` removes it, so the dashboard ratio always reflects each
    user's current verdict. A Message is reachable only through its
    conversation's owner, so another user's message 404s; user/system
    messages and unknown values 400 -- failures are surfaced, never
    silent.
    """
    message = get_object_or_404(
        Message.objects.filter(conversation__user=request.user),
        pk=_safe_pk(request.POST.get('message_id')),
    )
    value = str(request.POST.get('value') or '').strip()
    if value not in ('up', 'down', 'none'):
        return HttpResponseBadRequest('Unknown feedback value.')
    if message.role != Message.Role.ASSISTANT:
        return HttpResponseBadRequest('Feedback is collected on assistant messages only.')
    if value == 'none':
        MessageFeedback.objects.for_user(request.user).filter(message=message).delete()
        return JsonResponse({'message_id': message.pk, 'value': None})
    # Lookup keys mirror the uniqueness contract exactly -- passing the
    # user in the lookup (not a for_user queryset filter) is what keeps
    # the upsert from ever touching or creating another user's row.
    # An absent note leaves any stored note alone; an explicit (possibly
    # empty) one is the client saying "this is the note now".
    defaults = {'value': value}
    if request.POST.get('note') is not None:
        defaults['note'] = request.POST['note']
    feedback, _created = MessageFeedback.objects.update_or_create(
        message=message,
        user=request.user,
        defaults=defaults,
    )
    return JsonResponse({'message_id': message.pk, 'value': feedback.value})
