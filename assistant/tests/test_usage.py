"""UsageEvent recording tests: one row per completed authenticated turn.

The stream endpoint records usage once a turn produces content, so these
drive real AsyncClient turns over the scripted FakeLLM -- zero network.
Token counts may be zero (no usage data from the model); the row itself
is what makes calls countable on the dashboard.
"""

import asyncio
from unittest import mock

import pytest
from django.contrib.auth.models import User
from django.test import AsyncClient

from agent import loop as loop_module
from agent.tests.fakes import FakeLLM, delta
from assistant.models import ApiKey, UsageEvent

STREAM_URL = '/agent/stream/'


def fake_llm_script():
    """One scripted round: text only, no usage terminator (tokens stay 0)."""
    return FakeLLM([[delta('Hello '), delta(' fab.')]])


def post_turn(client, data, headers=None):
    """Post one stream turn and consume it so the loop finishes persisting."""

    async def _scenario():
        response = await client.post(STREAM_URL, data=data, headers=headers or {})
        body = b''.join([chunk async for chunk in response.streaming_content])
        return response, body

    return asyncio.run(_scenario())


@pytest.fixture
def user(transactional_db):
    return User.objects.create_user(username='alice', password='x')


def test_usage_event_written_for_key_authenticated_turn(user, monkeypatch):
    monkeypatch.setattr(loop_module, 'default_client', lambda: fake_llm_script())
    api_key, raw = ApiKey.generate(name='k', created_by=user)
    client = AsyncClient()

    response, _body = post_turn(client, {'message': 'hi'}, headers={'X-API-Key': raw})

    assert response.status_code == 200
    event = UsageEvent.objects.get()
    assert event.user == user
    assert event.api_key == api_key
    assert event.kind == UsageEvent.Kind.CHAT
    assert event.latency_ms >= 0


def test_usage_event_written_for_session_turn_without_api_key(user, monkeypatch):
    monkeypatch.setattr(loop_module, 'default_client', lambda: fake_llm_script())
    client = AsyncClient()
    client.force_login(user)

    response, _body = post_turn(client, {'message': 'hi'})

    assert response.status_code == 200
    event = UsageEvent.objects.get()
    assert event.user == user
    assert event.api_key is None


def test_missing_usage_data_records_zero_tokens(user, monkeypatch):
    monkeypatch.setattr(loop_module, 'default_client', lambda: fake_llm_script())
    _api_key, raw = ApiKey.generate(name='k', created_by=user)
    client = AsyncClient()

    post_turn(client, {'message': 'hi'}, headers={'X-API-Key': raw})
    post_turn(client, {'message': 'again'}, headers={'X-API-Key': raw})

    assert UsageEvent.objects.count() == 2
    for event in UsageEvent.objects.all():
        assert event.tokens_in + event.tokens_out == 0


def test_recording_failure_does_not_break_the_response(user, monkeypatch):
    monkeypatch.setattr(loop_module, 'default_client', lambda: fake_llm_script())
    _api_key, raw = ApiKey.generate(name='k', created_by=user)
    client = AsyncClient()

    with mock.patch.object(UsageEvent, 'objects') as manager:
        manager.create.side_effect = RuntimeError('database down')
        response, _body = post_turn(client, {'message': 'hi'}, headers={'X-API-Key': raw})

    assert response.status_code == 200
    manager.create.assert_called_once()
