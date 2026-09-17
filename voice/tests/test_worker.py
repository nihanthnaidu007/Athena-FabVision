"""Worker entrypoint wiring: room resolution, reconnect handling, session shape.

Zero-network: imports are the installed livekit packages, the RTC room is a
fake, and nothing calls connect(). DB-touching async tests use the
transactional_db pattern so sync_to_async threads see committed rows.
"""

import asyncio

import pytest
from django.contrib.auth.models import User

from agent.tests.fakes import async_test
from voice import flags
from voice.agent import AthenaVoiceAgent
from voice.worker import (
    _resolve_room_user_sync,
    attach_reconnect_handlers,
    resolve_room_user,
)

from .fakes import FakeRoom


def test_worker_imports_and_constructs_agent_session():
    """Pinned-deps smoke test: the worker imports and the pieces construct."""
    from livekit.agents import AgentSession

    from voice import worker

    assert callable(worker.entrypoint)
    assert callable(worker.prewarm)

    async def construct():
        return AgentSession(), AthenaVoiceAgent()

    session, agent = asyncio.run(construct())
    assert session is not None
    assert agent is not None


@pytest.fixture
def user(transactional_db):
    return User.objects.create_user(username='nina', password='x')


@async_test
async def test_resolves_the_room_owner(user):
    resolved = await resolve_room_user(flags.voice_room_name(user))
    assert resolved.pk == user.pk


def test_round_trip_through_the_flag_helpers(user):
    # Mint-side room naming and worker-side resolution stay in lockstep.
    resolved = _resolve_room_user_sync(flags.voice_room_name(user))
    assert resolved.pk == user.pk


def test_malformed_room_names_resolve_to_none(user):
    for room in (
        None,
        '',
        'chat-user-1',
        'voice-user-',
        'voice-user-abc',
        f'voice-user-{user.pk}-extra',
    ):
        assert _resolve_room_user_sync(room) is None, room


def test_unknown_or_inactive_users_resolve_to_none(user):
    assert _resolve_room_user_sync('voice-user-999999') is None
    user.is_active = False
    user.save()
    assert _resolve_room_user_sync(flags.voice_room_name(user)) is None


def test_handlers_fire_on_reconnect_events():
    room = FakeRoom('voice-user-1')
    events: list[str] = []
    attach_reconnect_handlers(
        room,
        on_reconnecting=lambda: events.append('reconnecting'),
        on_reconnected=lambda: events.append('reconnected'),
    )
    room.emit('reconnecting')
    room.emit('reconnected')
    assert events == ['reconnecting', 'reconnected']


def test_handlers_without_callbacks_still_attach():
    room = FakeRoom()
    attach_reconnect_handlers(room)  # logs only; must not raise
    room.emit('reconnecting')
    room.emit('reconnected')


def test_only_lifecycle_events_are_registered():
    room = FakeRoom()
    attach_reconnect_handlers(room)
    assert sorted(room.handlers) == ['reconnected', 'reconnecting']
