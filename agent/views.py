from django.shortcuts import render
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from .agent_logic import start_agent

class AgentAPIView(APIView):
    """
    API endpoint to handle user queries and return agent responses.
    """
    def post(self, request):
        query = request.data.get('query')
        if not query:
            return Response({"error": "Query parameter is required"}, status=status.HTTP_400_BAD_REQUEST)

        response = start_agent(query)
        return Response({"response": response})

def agent_home(request):
    """
    Serve the frontend template for the AI agent.
    """
    return render(request, 'agent/index.html')
