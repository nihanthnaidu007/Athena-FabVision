"""agent/views.py: SSE endpoint auth, throttling, CSRF, and streaming contract.

AsyncClient drives the real DRF stack for streaming responses; a sync
Client covers header assertions on unconsumed streams. All LLM traffic
goes through FakeLLM -- zero network.
"""

from __future__ import annotations

import asyncio
import json
import re

import pytest
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import AsyncClient, Client, override_settings

from agent import loop as loop_module
from assistant.models import ApiKey, Conversation, Message, UsageEvent

from .fakes import FakeLLM, delta, usage

SSE_URL = "/agent/stream/"


def throttle_settings(**rates):
    """Override specific throttle rates, keeping all other rates and DRF config."""
    base = settings.REST_FRAMEWORK
    return {
        **base,
        "DEFAULT_THROTTLE_RATES": {**base["DEFAULT_THROTTLE_RATES"], **rates},
    }


def parse_sse(body: bytes) -> list[tuple[str, dict]]:
    frames = []
    for frame in body.decode().split("\n\n"):
        if not frame:
            continue
        event_type = next(
            line.split(": ", 1)[1] for line in frame.splitlines() if line.startswith("event: ")
        )
        data_line = next(line for line in frame.splitlines() if line.startswith("data: "))
        frames.append((event_type, json.loads(data_line[len("data: ") :])))
    return frames


@pytest.fixture
def user(transactional_db):
    return User.objects.create_user(username="sse-user", password="x")


def fake_llm_script():
    # One round: deltas + usage terminator (no tool calls, stream ends).
    return FakeLLM([[delta("Hello "), delta("fab."), usage(4, 2)]])


def test_anonymous_request_is_401_with_error_contract(db):
    response = Client().post(SSE_URL, data={"message": "hi"})

    assert response.status_code == 401
    payload = response.json()
    assert payload["code"] == "authentication_required"
    assert payload["error"]
    assert payload["request_id"]


def test_invalid_api_key_is_rejected(db, user):
    response = Client().post(SSE_URL, data={"message": "hi"}, HTTP_X_API_KEY="bogus-key")

    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"


def test_missing_message_is_400(db, user):
    _api_key, raw = ApiKey.generate(name="k", created_by=user)
    response = Client().post(SSE_URL, data={}, HTTP_X_API_KEY=raw)

    assert response.status_code == 400
    assert response.json()["code"] == "validation_error"
    assert "message" in response.json()["error"]


def test_foreign_conversation_is_404(db, user):
    owner = User.objects.create_user(username="owner", password="x")
    other = Conversation.objects.create(user=owner)
    _api_key, raw = ApiKey.generate(name="k", created_by=user)

    response = Client().post(
        SSE_URL,
        data={"message": "hi", "conversation_id": other.pk},
        HTTP_X_API_KEY=raw,
    )

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_stream_headers_on_unconsumed_response(db, user, monkeypatch):
    monkeypatch.setattr(loop_module, "default_client", lambda: fake_llm_script())
    _api_key, raw = ApiKey.generate(name="k", created_by=user)

    response = Client().post(SSE_URL, data={"message": "hi"}, HTTP_X_API_KEY=raw)

    assert response.status_code == 200
    assert response["Content-Type"] == "text/event-stream"
    assert response["X-Accel-Buffering"] == "no"
    assert response["Cache-Control"] == "no-cache"


def test_full_turn_over_sse_records_usage_and_persists(transactional_db, user, monkeypatch):
    monkeypatch.setattr(loop_module, "default_client", lambda: fake_llm_script())
    api_key, raw = ApiKey.generate(name="k", created_by=user)
    client = AsyncClient()

    async def _scenario():
        # AsyncClient drops HTTP_-style kwargs -- use headers= for API keys.
        response = await client.post(
            SSE_URL,
            data={"message": "hi"},
            headers={"X-API-Key": raw},
        )
        body = b"".join([chunk async for chunk in response.streaming_content])
        return parse_sse(body)

    frames = asyncio.run(_scenario())

    types = [event_type for event_type, _ in frames]
    assert types[0] == "status"
    assert types[-4:] == ["delta", "delta", "sources", "done"]
    done_data = frames[-1][1]
    assert done_data["tokens_in"] == 4
    assert done_data["tokens_out"] == 2

    conversation = Conversation.objects.get(pk=done_data["conversation_id"])
    assert conversation.user == user
    assert conversation.messages.filter(role=Message.Role.USER, content="hi").exists()
    assert conversation.messages.filter(role=Message.Role.ASSISTANT, content="Hello fab.").exists()
    event = UsageEvent.objects.get()
    assert event.api_key == api_key
    assert event.kind == "chat"


def test_session_auth_without_csrf_token_is_403(transactional_db, user):
    client = AsyncClient(enforce_csrf_checks=True)
    client.force_login(user)  # sync context -- sync ORM allowed here

    async def _scenario():
        return await client.post(SSE_URL, data={"message": "hi"})

    response = asyncio.run(_scenario())

    assert response.status_code == 403


def test_session_csrf_token_from_template_satisfies_check(transactional_db, user, monkeypatch):
    monkeypatch.setattr(loop_module, "default_client", lambda: None)  # degraded, no key
    client = AsyncClient(enforce_csrf_checks=True)
    client.force_login(user)

    async def _scenario():
        page = await client.get("/")  # the chat page renders the CSRF meta tag
        match = re.search(rb'name="csrf-token" content="([^"]+)"', page.content)
        assert match, "chat template must render the CSRF token"
        response = await client.post(
            SSE_URL,
            data={"message": "hi"},
            headers={"x-csrftoken": match.group(1).decode()},
        )
        body = b"".join([chunk async for chunk in response.streaming_content])
        return parse_sse(body)

    frames = asyncio.run(_scenario())

    # With no API key configured the turn streams the degraded error
    # contract instead of crashing -- zero-key boot end to end.
    assert frames[-1][0] == "error"
    assert frames[-1][1]["code"] == "llm_unavailable"


@override_settings(REST_FRAMEWORK=throttle_settings(user="1/hour"))
def test_throttled_session_requests_get_429_and_retry_after(transactional_db, user):
    cache.clear()
    client = AsyncClient()
    client.force_login(user)

    async def _scenario():
        first = await client.post(SSE_URL, data={"message": "hi"})
        second = await client.post(SSE_URL, data={"message": "hi again"})
        return first, second

    first, second = asyncio.run(_scenario())

    assert first.status_code == 200
    assert second.status_code == 429
    assert "Retry-After" in second.headers
    assert second.json()["code"] == "rate_limited"


@override_settings(REST_FRAMEWORK=throttle_settings(api_key_standard="1/hour"))
def test_throttled_api_key_requests_get_429(db, user):
    cache.clear()
    _api_key, raw = ApiKey.generate(name="k", created_by=user)
    client = AsyncClient()

    async def _scenario():
        first = await client.post(
            SSE_URL,
            data={"message": "hi"},
            headers={"X-API-Key": raw},
        )
        second = await client.post(
            SSE_URL,
            data={"message": "hi again"},
            headers={"X-API-Key": raw},
        )
        return first, second

    first, second = asyncio.run(_scenario())

    assert first.status_code == 200
    assert second.status_code == 429
    assert "Retry-After" in second.headers
