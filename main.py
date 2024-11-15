import asyncio
import logging
from dotenv import load_dotenv
from livekit.agents import AutoSubscribe, JobContext, WorkerOptions, cli, llm
from livekit.agents.voice_assistant import VoiceAssistant
from livekit.plugins import openai, silero
from api import AssistantFnc

# Load environment variables
load_dotenv()

# Set up structured logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

class VoiceAssistantError(Exception):
    """Custom exception for VoiceAssistant errors."""
    pass

def load_instructions(file_path):
    """Read instructions from a text file."""
    try:
        with open(file_path, 'r', encoding='utf-8') as file:
            return file.read()
    except (FileNotFoundError, IOError) as e:
        logger.error("Error loading instructions file: %s", e)
        raise VoiceAssistantError("Failed to load instructions file") from e

async def entrypoint(ctx: JobContext):
    """Entrypoint for the voice assistant with error handling and new features."""
    try:
        # Load the instructions synchronously
        instructions = load_instructions("instructions.txt")

        # Create the initial context for the LLM
        initial_ctx = llm.ChatContext().append(role="system", text=instructions)

        # Connect to the room with audio-only subscription
        await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
        fnc_ctx = AssistantFnc()

        # Initialize the voice assistant with required components
        assistant = VoiceAssistant(
            vad=silero.VAD.load(),
            stt=openai.STT(),
            llm=openai.LLM(),
            tts=openai.TTS(),
            chat_ctx=initial_ctx,
            fnc_ctx=fnc_ctx,
        )

        # Start the assistant and greet the user
        assistant.start(ctx.room)
        await assistant.say("Hi, this is Athena. How can I help you?", allow_interruptions=True)

        # Listen and respond to user inputs
        while True:
            user_input = await assistant.listen()  # Listen for user input

            # Check for time and date requests
            if "time" in user_input.lower() or "date" in user_input.lower():
                datetime_info = fnc_ctx.get_current_datetime()
                await assistant.say(datetime_info, allow_interruptions=True)
            # Check for joke requests
            elif "joke" in user_input.lower():
                joke = fnc_ctx.tell_joke()
                await assistant.say(joke, allow_interruptions=True)
            # Check for insight requests
            elif "insight" in user_input.lower() or "tell me about" in user_input.lower():
                topic = user_input.split("about")[-1].strip() if "about" in user_input else "general"
                insight = fnc_ctx.provide_insight(topic)
                await assistant.say(insight, allow_interruptions=True)
            # Web search for unknown requests
            else:
                web_result = fnc_ctx.search_web(user_input)
                await assistant.say(web_result, allow_interruptions=True)

    except VoiceAssistantError as e:
        logger.critical("Critical error occurred: %s", e)
    except Exception as e:
        logger.error("Unexpected error: %s", e)
    finally:
        logger.info("Shutting down VoiceAssistant")

if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
