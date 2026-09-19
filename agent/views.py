import json
import time
from typing import Any

from asgiref.sync import async_to_sync
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
from django.utils.text import slugify
from django.views.decorators.http import require_POST
from rest_framework import exceptions
from rest_framework.negotiation import DefaultContentNegotiation
from rest_framework.renderers import JSONRenderer
from rest_framework.views import APIView

from assistant.models import (
    ApiKey,
    Conversation,
    Message,
    MessageFeedback,
    Notebook,
    UsageEvent,
)
from assistant.usage import record_usage
from django_agent.logging_context import get_request_id

from .export import conversation_to_markdown, rca_report_to_markdown
from .llm import default_client
from .loop import run_agent
from .rca import assemble_report


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

    Body: ``{"message": "...", "conversation_id": <optional pk>,
    "notebook_id": <optional pk>}`` (the legacy ``query`` field is
    accepted too). ``notebook_id`` scopes a NEW conversation to a
    notebook: retrieval then narrows to that notebook's documents. It is
    honored only when the stream creates the conversation -- an existing
    conversation keeps its own notebook. A malformed or foreign
    notebook id is "not found", the same semantics as conversation ids.
    ``mode`` (optional, ``assistant``|``tutor``) sets a newly created
    conversation's mode and is honored only at creation -- an existing
    conversation's persisted mode always rules; the chat-mode endpoint
    changes it. The response streams
    ``event: <type>`` / ``data: <json>`` frames for status, delta,
    tool_call, tool_result, sources, turn_saved (the persisted assistant
    message's pk, the per-message feedback hook), done, and error events;
    failures inside the turn are error events, never a broken stream.
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
                notebook=_resolve_notebook(request),
            )

        request_id = getattr(request, 'request_id', '')
        return _sse_response(
            user=request.user,
            conversation=conversation,
            user_input=user_input,
            request_id=request_id,
            api_key=request.auth if isinstance(request.auth, ApiKey) else None,
            notebook_id=conversation.notebook_id,
        )


class AgentRegenerateView(APIView):
    """POST /agent/chat/<pk>/regenerate/ -- a fresh answer for the last turn.

    Same auth, throttling, and SSE contract as /agent/stream/. The
    conversation's trailing assistant message is deleted (its feedback
    rows cascade with it) and the turn is regenerated from the user row
    that produced it: ``existing_user_message`` reuses that row, so
    regenerating never duplicates the user message. A conversation whose
    final row is not an answer (empty, or a turn that failed before
    producing output) answers 400 -- there is nothing to regenerate.
    """

    renderer_classes = [JSONRenderer]
    content_negotiation_class = EventStreamNegotiation

    def post(self, request, pk):
        conversation = get_object_or_404(Conversation.objects.for_user(request.user), pk=pk)
        last_row = conversation.messages.order_by('-pk').first()
        if last_row is None or last_row.role != Message.Role.ASSISTANT:
            raise exceptions.ValidationError('Nothing to regenerate yet — send a message first.')
        user_message = (
            conversation.messages.filter(role=Message.Role.USER, pk__lt=last_row.pk)
            .order_by('-pk')
            .first()
        )
        if user_message is None:
            # Unreachable through the product (a user row always precedes
            # its answer), but an honest 400 beats a fabricated turn.
            raise exceptions.ValidationError('The last answer has no user turn to regenerate.')
        last_row.delete()
        return _sse_response(
            user=request.user,
            conversation=conversation,
            user_input=user_message.content,
            request_id=getattr(request, 'request_id', ''),
            api_key=request.auth if isinstance(request.auth, ApiKey) else None,
            existing_user_message=user_message,
            notebook_id=conversation.notebook_id,
        )


def _sse_response(**kwargs: Any) -> StreamingHttpResponse:
    """Wrap ``run_agent``'s events in the SSE response both streaming
    endpoints return (``/agent/stream/`` and ``.../regenerate/``)."""
    response = StreamingHttpResponse(
        agent_sse_stream(**kwargs),
        content_type='text/event-stream',
    )
    response['Cache-Control'] = 'no-cache'
    # Disable proxy buffering (nginx et al.) so deltas arrive live.
    response['X-Accel-Buffering'] = 'no'
    return response


def _resolve_notebook(request) -> Notebook | None:
    """Resolve the stream body's ``notebook_id`` for a new conversation.

    Absent or empty means unscoped (whole knowledge base). A malformed,
    unknown, or foreign id raises ``Http404`` -- identical semantics to
    conversation ids -- so scoping can never widen another user's data.
    """
    raw = request.data.get('notebook_id')
    if raw in (None, ''):
        return None
    notebook_id = _safe_pk(str(raw))
    return get_object_or_404(Notebook.objects.for_user(request.user), pk=notebook_id)


# --- Chat UI -----------------------------------------------------------------
# Server-rendered per the locked frontend decision (no JS build step): the
# page renders the conversation sidebar and the selected thread; chat.js
# hydrates markdown, citation chips, and tool blocks client-side and
# consumes the SSE stream. Every read and mutation goes through the
# user-scoped querysets from assistant.models, so cross-user access 404s.

SIDEBAR_CONVERSATION_LIMIT = 50


def _message_items(messages: list[Message], user: Any) -> list[dict[str, Any]]:
    """View-model rows for the thread template.

    Each row carries deterministic ``json_script`` element ids so
    ``chat.js`` can hydrate markdown, citation chips, and tool blocks
    from the persisted JSON fields. Assistant rows also carry the
    user's saved feedback verdict (``''`` when none), which the client
    renders as the active thumb -- one extra query for the whole page.
    """
    feedback_by_message: dict[int, str] = {}
    assistant_pks = [message.pk for message in messages if message.role == Message.Role.ASSISTANT]
    if user is not None and assistant_pks:
        feedback_by_message = dict(
            MessageFeedback.objects.for_user(user)
            .filter(message_id__in=assistant_pks)
            .values_list('message_id', 'value')
        )
    items: list[dict[str, Any]] = []
    for message in messages:
        pk = str(message.pk)
        items.append(
            {
                'message': message,
                'content_id': f'msg-{pk}-content',
                'sources_id': f'msg-{pk}-sources',
                'blocks_id': f'msg-{pk}-blocks',
                'feedback_value': feedback_by_message.get(message.pk, ''),
            }
        )
    return items


def _conversation_items(user: Any, active_pk: int | None) -> list[dict[str, Any]]:
    """Sidebar rows; ``is_active`` marks the conversation being viewed."""
    # select_related('notebook') feeds the sidebar's notebook badge
    # without one query per row.
    conversations = list(
        Conversation.objects.for_user(user).select_related('notebook')[:SIDEBAR_CONVERSATION_LIMIT]
    )
    return [
        {'conversation': conversation, 'is_active': conversation.pk == active_pk}
        for conversation in conversations
    ]


def _sidebar_hidden_count(user: Any) -> int:
    """Conversations beyond the sidebar cap -- the indicator's count.

    The list silently truncates at SIDEBAR_CONVERSATION_LIMIT rows; the
    page says so instead of pretending the oldest chats vanished.
    """
    total = Conversation.objects.for_user(user).count()
    return max(total - SIDEBAR_CONVERSATION_LIMIT, 0)


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
            'sidebar_hidden_count': _sidebar_hidden_count(request.user),
            'sidebar_limit': SIDEBAR_CONVERSATION_LIMIT,
            'active_conversation': active_conversation,
            'message_items': _message_items(messages, request.user),
            'llm_configured': default_client() is not None,
            # Composer notebook scope (v1.1 #5): new conversations can
            # start scoped; an active conversation renders its own value.
            'notebooks': list(Notebook.objects.for_user(request.user).order_by('name')),
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
def rename_conversation(request: HttpRequest) -> HttpResponse:
    """Rename one of the user's own conversations inline (spec #9).

    Body: ``conversation_id`` plus ``title``; answers JSON with the
    persisted title. Blank titles are rejected (a rename is a deliberate
    edit, unlike creating, where a blank title defaults); foreign
    conversations 404.
    """
    conversation = get_object_or_404(
        Conversation.objects.for_user(request.user),
        pk=_safe_pk(request.POST.get('conversation_id')),
    )
    title = str(request.POST.get('title') or '').strip()[:200]
    if not title:
        return HttpResponseBadRequest('A non-empty title is required.')
    conversation.title = title
    conversation.save(update_fields=['title', 'updated_at'])
    return JsonResponse({'conversation_id': conversation.pk, 'title': conversation.title})


@login_required
def export_conversation(request: HttpRequest, pk: int) -> HttpResponse:
    """Download one of the user's conversations as Markdown (spec #9).

    The document is assembled by agent/export.py -- the same plumbing
    the RCA report generator reuses -- and served as an attachment; a
    GET is safe here because exporting changes nothing.
    """
    conversation = get_object_or_404(Conversation.objects.for_user(request.user), pk=pk)
    markdown = conversation_to_markdown(conversation, list(conversation.messages.all()))
    slug = slugify(conversation.title) or 'conversation'
    response = HttpResponse(markdown, content_type='text/markdown; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{slug}-{conversation.pk}.md"'
    return response


@login_required
@require_POST
def generate_rca_report(request: HttpRequest, pk: int) -> HttpResponse:
    """Assemble and download an 8D report from one conversation (spec #11).

    One budgeted LLM call per click (``agent.rca.assemble_report``),
    metered as an ``rca_report`` usage event; the draft renders through
    the same Markdown plumbing as the conversation export above. The
    chat UI hides the action on zero-key deployments, and the endpoint
    still answers 503 if POSTed directly. A model reply that fails the
    schema is an honest 502 naming the problem -- the report is never
    faked from nothing.
    """
    conversation = get_object_or_404(Conversation.objects.for_user(request.user), pk=pk)
    messages = list(conversation.messages.all())
    if not messages:
        return JsonResponse(
            {
                'code': 'conversation_empty',
                'error': 'Nothing to assemble from yet — this conversation has no messages.',
            },
            status=400,
        )
    llm = default_client()
    if llm is None:
        return JsonResponse(
            {'code': 'llm_unavailable', 'error': 'The model is not configured on this deployment.'},
            status=503,
        )
    transcript = conversation_to_markdown(conversation, messages)
    started = time.monotonic()
    assembly = async_to_sync(assemble_report)(
        title=conversation.title or '', transcript=transcript, llm=llm
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    if assembly.report is None:
        return JsonResponse({'code': assembly.code, 'error': assembly.error}, status=502)
    record_usage(
        user=request.user,
        kind=UsageEvent.Kind.TOOL,
        conversation=conversation,
        tool_name='rca_report',
        tokens_in=assembly.usage.get('tokens_in', 0),
        tokens_out=assembly.usage.get('tokens_out', 0),
        latency_ms=latency_ms,
        model_name=getattr(llm, 'model_name', ''),
    )
    markdown = rca_report_to_markdown(assembly.report)
    slug = slugify(conversation.title) or 'conversation'
    response = HttpResponse(markdown, content_type='text/markdown; charset=utf-8')
    response['Content-Disposition'] = f'attachment; filename="{slug}-8d-report.md"'
    return response


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
