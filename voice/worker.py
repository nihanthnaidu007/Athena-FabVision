"""Standalone LiveKit voice worker for Athena.

Runs as its own process (``python -m voice.worker``, Procfile entry
``voice``) and never blocks the web service: the Django web process does not
import this module. Requires LIVEKIT_URL / LIVEKIT_API_KEY /
LIVEKIT_API_SECRET -- without them the voice surface is not deployed at all
(the web app renders its setup guide instead).

Reconnect handling: LiveKit's protocol resumes the room across signal
drops; the handlers attached here keep the logs honest and re-announce on
recovery rather than treating a reconnect as a new session.
"""

from __future__ import annotations

import asyncio
import logging
import os

# LiveKit imports at module scope are network-free; only connect() and
# VAD.load() touch a server, and they run solely in the worker process.
from livekit.agents import (
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    RoomOutputOptions,
    WorkerOptions,
    cli,
)
from livekit.plugins import openai as openai_plugin
from livekit.plugins import silero

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'django_agent.settings')

logger = logging.getLogger(__name__)

RESUME_ANNOUNCEMENT = "Reconnected -- I'm still here. Go ahead."


def prewarm(proc: JobProcess) -> None:
    """Load the VAD once per worker process, not once per session."""
    proc.userdata['vad'] = silero.VAD.load()


def _resolve_room_user_sync(room_name: str | None):
    """Resolve the account a voice room is scoped to, or None (degraded room)."""
    from django.contrib.auth import get_user_model

    from .flags import VOICE_ROOM_PREFIX

    if not room_name or not room_name.startswith(VOICE_ROOM_PREFIX):
        return None
    raw = room_name[len(VOICE_ROOM_PREFIX) :]
    if not raw.isdigit():
        return None
    return get_user_model().objects.filter(pk=int(raw), is_active=True).first()


async def resolve_room_user(room_name: str | None):
    """Async wrapper around the room-name -> user resolution (ORM off-thread)."""
    from asgiref.sync import sync_to_async

    return await sync_to_async(_resolve_room_user_sync)(room_name)


def attach_reconnect_handlers(room, *, on_reconnecting=None, on_reconnected=None) -> None:
    """Wire graceful reconnect handling onto the RTC room.

    The room object keeps the session alive across a signal drop; these
    handlers log the transition and give the caller a hook to re-announce
    (``on_reconnected``) without treating the reconnect as a new session.
    """
    from .flags import VOICE_ROOM_PREFIX

    room_label = getattr(room, 'name', '') or f'{VOICE_ROOM_PREFIX}<session>'

    # rtc.Room emits both events as bare strings, with no payload.
    @room.on('reconnecting')
    def _on_reconnecting() -> None:
        logger.warning('Voice room %r reconnecting; the session will resume.', room_label)
        if on_reconnecting:
            on_reconnecting()

    @room.on('reconnected')
    def _on_reconnected() -> None:
        logger.info('Voice room %r reconnected.', room_label)
        if on_reconnected:
            on_reconnected()


async def entrypoint(ctx: JobContext) -> None:
    """One voice job: connect to the room, then run the shared-core agent."""
    from django.conf import settings

    from .agent import AthenaVoiceAgent

    if not settings.OPENAI_API_KEY:
        logger.warning(
            'OPENAI_API_KEY is not set: the voice agent runs degraded '
            '(shared-core tools answer honest errors, plugins may fail).'
        )

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    user = await resolve_room_user(ctx.room.name)

    conversation = None
    if user is not None:
        conversation = await _create_voice_conversation(user)
        logger.info(
            'Voice session starting (room=%s user=%s conversation=%s).',
            ctx.room.name,
            user.pk,
            conversation.pk,
        )
    else:
        logger.warning(
            'Voice room %r has no resolvable user; running a degraded session.',
            ctx.room.name,
        )

    session = AgentSession(
        stt=openai_plugin.STT(),
        llm=openai_plugin.LLM(model=settings.OPENAI_CHAT_MODEL),
        tts=openai_plugin.TTS(),
        vad=ctx.proc.userdata['vad'],
    )

    def _announce_resume() -> None:
        asyncio.ensure_future(session.say(RESUME_ANNOUNCEMENT))

    attach_reconnect_handlers(ctx.room, on_reconnected=_announce_resume)

    await session.start(
        room=ctx.room,
        agent=AthenaVoiceAgent(user=user, conversation=conversation),
        room_output_options=RoomOutputOptions(transcription_enabled=True),
    )
    await session.generate_reply(
        instructions='Greet the user in one short sentence and offer semiconductor fab help.'
    )


def _create_voice_conversation(user):
    """Create the voice session's conversation off the event loop."""
    from asgiref.sync import sync_to_async

    from assistant.models import Conversation

    from .flags import voice_room_name

    return sync_to_async(Conversation.objects.create)(user=user, title=voice_room_name(user))


def main() -> None:
    """Boot Django, then hand over to the LiveKit worker CLI."""
    import django

    django.setup()
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))


if __name__ == '__main__':
    main()
