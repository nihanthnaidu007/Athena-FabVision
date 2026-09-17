from django.shortcuts import render
from rest_framework import exceptions
from rest_framework.response import Response
from rest_framework.views import APIView

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

        response = start_agent(query)
        return Response({"response": response})

def agent_home(request):
    """
    Serve the frontend template for the AI agent.
    """
    return render(request, 'agent/index.html')
