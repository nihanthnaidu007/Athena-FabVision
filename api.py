import random
import logging
import datetime
import requests
import os
from dotenv import load_dotenv
from livekit.agents import llm

# Load environment variables
load_dotenv()

# Set up structured logging
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
            "I tried to read a semiconductor book on electrons... but it was over my potential."
        ]
        joke = random.choice(jokes)
        logger.info("Telling a joke: %s", joke)
        return joke

    @llm.ai_callable(description="Provide an insightful fact based on the topic provided.")
    def provide_insight(self, topic: str) -> str:
        """Provide insight or knowledge based on the topic."""
        insights = {
            "semiconductors": "Semiconductors are the backbone of modern electronics, enabling the miniaturization of circuits.",
            "AI": "Artificial intelligence is transforming industries by making systems more adaptive, efficient, and insightful.",
            "optimization": "Optimization in manufacturing reduces waste, improves efficiency, and maximizes yield, crucial in high-stakes semiconductor production.",
            "manufacturing": "In semiconductor manufacturing, precision and process control are key to ensuring high yield and minimal defects."
        }
        insight = insights.get(topic.lower(), "I'm always here to provide insights on semiconductors, AI, and optimization!")
        logger.info("Providing insight on topic '%s': %s", topic, insight)
        return insight

    @llm.ai_callable(description="Get the current date and time.")
    def get_current_datetime(self) -> str:
        """Return the current date and time."""
        now = datetime.datetime.now()
        formatted_time = now.strftime("%Y-%m-%d %H:%M:%S")
        logger.info("Providing current date and time: %s", formatted_time)
        return f"The current date and time is: {formatted_time}"

    @llm.ai_callable(description="Search the web for a query when information is not in the knowledge base.")
    def search_web(self, query: str) -> str:
        """Search the web for information if not available in knowledge base."""
        logger.info("Searching web for query: %s", query)
        
        # Load API credentials from environment variables
        api_key = os.getenv("GOOGLE_API_KEY")
        search_engine_id = os.getenv("GOOGLE_SEARCH_ENGINE_ID")
        search_url = f"https://www.googleapis.com/customsearch/v1?key={api_key}&cx={search_engine_id}&q={query}"

        try:
            response = requests.get(search_url)
            response.raise_for_status()
            data = response.json()

            # Log full response for debugging purposes
            logger.debug("Full response from web search API: %s", data)

            # Check if 'items' key is in the response
            if "items" in data and len(data["items"]) > 0:
                # Use the first search result's snippet as the answer
                result = data["items"][0]["snippet"]
                logger.info("Web search result: %s", result)
                return result
            elif "error" in data:
                # Handle specific error responses from the API
                error_message = data["error"].get("message", "An unknown error occurred.")
                logger.error("API error message: %s", error_message)
                return f"I'm currently unable to access the latest information due to an API issue: {error_message}"
            else:
                logger.info("No results found for query: %s", query)
                return "I'm sorry, I couldn't find any information on that topic."
        
        except requests.RequestException as e:
            logger.error("Error during web search request: %s", e)
            return "I encountered a network error while searching the web. Please try again later."
        except Exception as e:
            logger.error("Unexpected error in search_web: %s", e)
            return "An unexpected error occurred while searching the web. Please try again later."
