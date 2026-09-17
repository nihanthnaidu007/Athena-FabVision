import json
import time

from django.http import StreamingHttpResponse
from django.shortcuts import get_object_or_404, render
from rest_framework import exceptions
from rest_framework.response import Response
from rest_framework.views import APIView

from assistant.models import ApiKey, Conversation, UsageEvent
from assistant.usage import record_usage

from .agent_logic import start_agent
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
    Serve the frontend template for the AI agent.
    """
    return render(request, 'agent/index.html')


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
