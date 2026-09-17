import time

from django.shortcuts import render
from rest_framework import exceptions
from rest_framework.response import Response
from rest_framework.views import APIView

from assistant.models import ApiKey, UsageEvent
from assistant.usage import record_usage

from .agent_logic import start_agent


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
