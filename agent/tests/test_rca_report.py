"""agent.rca + the generate_rca_report view: the 8D report action (spec #11).

One budgeted LLM call per click, scripted here through FakeLLM -- zero
network. Pins: user isolation, the honest failure states (empty
conversation, no LLM, schema-invalid reply, failing stream), the
``rca_report`` usage metering, and the Markdown download itself.
"""

from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse

from agent import views as views_module
from assistant.models import Conversation, Message, UsageEvent

from .fakes import FakeLLM, delta, usage

pytestmark = pytest.mark.django_db


@pytest.fixture
def user() -> User:
    return User.objects.create_user(username="rca-user", password="pw-123456")


@pytest.fixture
def other_user() -> User:
    return User.objects.create_user(username="rca-other", password="pw-123456")


@pytest.fixture
def excursion_conversation(user) -> Conversation:
    """An investigation conversation: user turn, triage answer, findings."""
    conversation = Conversation.objects.create(user=user, title="Lot 42A excursion")
    Message.objects.create(
        conversation=conversation, role=Message.Role.USER, content="Lot 42A yield is 62%."
    )
    Message.objects.create(
        conversation=conversation,
        role=Message.Role.ASSISTANT,
        content="The triage flags a critical yield excursion.",
    )
    return conversation


def valid_reply() -> str:
    return json.dumps(
        {
            "title": "Lot 42A edge-ring excursion",
            "team": ["Process engineering"],
            "problem": "Edge-ring yield loss after the etch chamber PM.",
            "timeline": [
                {"when": "2026-09-14", "event": "PM completed; first post-PM lot started."}
            ],
            "containment": ["Held lot 42A."],
            "root_cause": "PM left a drifted gap setting.",
            "corrective_actions": [
                {"action": "Recalibrate the gap setting.", "owner": "Etch owner"}
            ],
        }
    )


def scripted_llm(reply: str) -> FakeLLM:
    # One round: the reply in two deltas, then the usage terminator.
    middle = len(reply) // 2
    return FakeLLM([[delta(reply[:middle]), delta(reply[middle:]), usage(50, 20)]])


def test_anonymous_is_redirected_to_login(db):
    response = __import__("django").test.Client().post(reverse("chat-rca-report", args=[1]))

    assert response.status_code == 302
    assert "/dashboard/accounts/login/" in response["Location"]


def test_get_is_not_allowed(client, user, excursion_conversation):
    client.force_login(user)

    response = client.get(reverse("chat-rca-report", args=[excursion_conversation.pk]))

    assert response.status_code == 405


def test_other_users_conversation_is_404(client, user, other_user, excursion_conversation):
    client.force_login(other_user)

    response = client.post(reverse("chat-rca-report", args=[excursion_conversation.pk]))

    assert response.status_code == 404


def test_empty_conversation_answers_400(client, user):
    conversation = Conversation.objects.create(user=user, title="nothing yet")
    client.force_login(user)

    response = client.post(reverse("chat-rca-report", args=[conversation.pk]))

    assert response.status_code == 400
    assert response.json()["code"] == "conversation_empty"


@override_settings(OPENAI_API_KEY=None)
def test_zero_key_deployment_answers_503(client, user, excursion_conversation):
    client.force_login(user)

    response = client.post(reverse("chat-rca-report", args=[excursion_conversation.pk]))

    assert response.status_code == 503
    assert response.json()["code"] == "llm_unavailable"


def test_report_assembles_from_scripted_llm_and_downloads(
    client, user, excursion_conversation, monkeypatch
):
    llm = scripted_llm(valid_reply())
    monkeypatch.setattr(views_module, "default_client", lambda: llm)
    client.force_login(user)

    response = client.post(reverse("chat-rca-report", args=[excursion_conversation.pk]))

    assert response.status_code == 200
    assert response["Content-Type"] == "text/markdown; charset=utf-8"
    assert 'filename="lot-42a-excursion-8d-report.md"' in response["Content-Disposition"]
    body = response.content.decode()
    assert "# Lot 42A edge-ring excursion" in body
    for section in ("D1", "D2", "D3", "D4", "D5", "D6", "D7", "D8"):
        assert f"## {section}" in body


def test_assembly_sends_the_conversation_transcript_to_the_llm(
    client, user, excursion_conversation, monkeypatch
):
    llm = scripted_llm(valid_reply())
    monkeypatch.setattr(views_module, "default_client", lambda: llm)
    client.force_login(user)

    client.post(reverse("chat-rca-report", args=[excursion_conversation.pk]))

    # Exactly one LLM call, and its user message carries the transcript.
    assert len(llm.calls) == 1
    prompt = llm.calls[0]["messages"]
    assert [message["role"] for message in prompt] == ["system", "user"]
    assert "Lot 42A yield is 62%." in prompt[1]["content"]


def test_report_call_is_metered_as_a_tool_usage_event(
    client, user, excursion_conversation, monkeypatch
):
    llm = scripted_llm(valid_reply())
    monkeypatch.setattr(views_module, "default_client", lambda: llm)
    client.force_login(user)

    response = client.post(reverse("chat-rca-report", args=[excursion_conversation.pk]))

    assert response.status_code == 200
    event = UsageEvent.objects.get()
    assert event.user == user
    assert event.kind == UsageEvent.Kind.TOOL
    assert event.tool_name == "rca_report"
    assert event.conversation == excursion_conversation
    assert event.tokens_in == 50
    assert event.tokens_out == 20


def test_invalid_model_reply_answers_502_and_meters_nothing(
    client, user, excursion_conversation, monkeypatch
):
    monkeypatch.setattr(
        views_module, "default_client", lambda: scripted_llm("I cannot assemble a report.")
    )
    client.force_login(user)

    response = client.post(reverse("chat-rca-report", args=[excursion_conversation.pk]))

    assert response.status_code == 502
    payload = response.json()
    assert payload["code"] == "report_invalid"
    assert "no JSON object" in payload["error"]
    assert UsageEvent.objects.count() == 0


def test_schema_violating_reply_answers_502_naming_the_field(
    client, user, excursion_conversation, monkeypatch
):
    incomplete = json.dumps({"title": "Only a title"})
    monkeypatch.setattr(
        views_module, "default_client", lambda: scripted_llm(incomplete)
    )
    client.force_login(user)

    response = client.post(reverse("chat-rca-report", args=[excursion_conversation.pk]))

    assert response.status_code == 502
    payload = response.json()
    assert payload["code"] == "report_invalid"
    assert "'problem'" in payload["error"]


def test_failing_stream_answers_502_generation_failed(
    client, user, excursion_conversation, monkeypatch
):
    monkeypatch.setattr(
        views_module, "default_client", lambda: FakeLLM([[RuntimeError("upstream down")]])
    )
    client.force_login(user)

    response = client.post(reverse("chat-rca-report", args=[excursion_conversation.pk]))

    assert response.status_code == 502
    payload = response.json()
    assert payload["code"] == "report_generation_failed"
    assert "upstream down" in payload["error"]
    assert UsageEvent.objects.count() == 0
