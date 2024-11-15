import random
import logging
from livekit.agents import llm  # Ensure this import is correct and livekit is installed in your environment

# Configure structured logging
logger = logging.getLogger("assistant-fnc")
logger.setLevel(logging.INFO)

class AssistantFnc(llm.FunctionContext):
    """Class containing AI-callable functions for the AI assistant."""

    @llm.ai_callable(description="Tell a random joke to lighten the mood.")
    def tell_joke(self) -> str:
        """Return a random joke."""
        jokes = [
            "Why do we never tell secrets on a semiconductor wafer? Because it might leak!",
            "Why did the photon check into a hotel? Because it needed to rest its wave function!",
            "Why do programmers hate nature? Because it has too many bugs!",
            "I tried to read a semiconductor book on electrons... but it was over my potential.",
        ]
        joke = random.choice(jokes)
        logger.info("Telling a joke: %s", joke)
        return joke

def start_agent(query):
    """
    Function to handle user queries and return the agent's response.
    It decides what response to give based on the query.
    """
    assistant = AssistantFnc()
    if "joke" in query.lower():
        response = assistant.tell_joke()
    else:
        response = "Sorry, I can only tell jokes for now!"
    logger.info("Agent Response: %s", response)
    return response
