"""Legacy /agent/ask/ fallback: keyword-routed canned responses.

The 0.x-era AssistantFnc (``livekit.agents.llm.FunctionContext`` /
``llm.ai_callable``) was removed in the livekit-agents 1.x migration -- the
real voice surface now lives in ``voice/agent.py`` through the shared agent
core (agent.loop + registry). This shim only preserves the historical
/demo-grade/ behavior of the /agent/ask/ REST endpoint.
"""

import logging
import random

logger = logging.getLogger(__name__)

JOKES = [
    'Why did the photon check into a hotel? It was traveling light.',
    'Why did the process engineer bring a ladder to the fab? To reach the next node.',
    'Why did the wafer go to therapy? It had too many defects to deal with.',
]


def start_agent(query):
    """Handle a /agent/ask/ query the way the legacy fallback always has."""
    if 'joke' in query.lower():
        response = random.choice(JOKES)
    else:
        response = 'Sorry, I can only tell jokes for now!'
    logger.info('Agent Response: %s', response)
    return response
