"""Chat UI: the chat page template/view contract and conversation CRUD.

Zero-network: no LLM or retrieval traffic. The vanilla-JS client's inputs
are pinned here as rendered contracts (data attributes, ``json_script``
payloads, form actions), because the repo has no node/jsdom toolchain by
the locked frontend decision -- client behavior itself is covered by a
manual browser smoke documented in the PR.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.test import override_settings
from django.urls import reverse
from django.utils import timezone

from assistant.models import Conversation, Message

pytestmark = pytest.mark.django_db


@pytest.fixture
def user() -> User:
    return User.objects.create_user(username="chat-user", password="pw-123456")


@pytest.fixture
def other_user() -> User:
    return User.objects.create_user(username="chat-other", password="pw-123456")


def make_conversation(user: User, title: str = "conv") -> Conversation:
    return Conversation.objects.create(user=user, title=title)


def add_message(conversation: Conversation, **fields) -> Message:
    return Message.objects.create(conversation=conversation, **fields)


def test_anonymous_chat_home_redirects_to_login(client):
    """``/`` is the login-required chat page; anonymous users are bounced."""
    response = client.get("/")

    assert response.status_code == 302
    assert "/dashboard/accounts/login/" in response["Location"]


def test_chat_home_renders_client_contract(client, user):
    """The page exposes every URL and hook the chat client consumes."""
    client.force_login(user)

    response = client.get(reverse("chat-home"))

    html = response.content.decode()
    assert response.status_code == 200
    assert '<meta name="csrf-token"' in html
    assert 'id="chat-root"' in html
    assert 'data-stream-url="/agent/stream/"' in html
    assert 'data-upload-url="/kb/documents/"' in html
    assert 'data-new-chat-url="/agent/chat/new/"' in html
    assert 'data-mode-url="/agent/chat/mode/"' in html
    assert 'id="composer-form"' in html
    assert 'id="stop-button"' in html
    assert 'id="kb-file-input"' in html
    assert 'accept=".txt,.md,.pdf"' in html
    assert 'id="conversation-list"' in html


def test_chat_home_shows_only_own_conversations(client, user, other_user):
    """The sidebar is user-scoped: other users' conversations never appear."""
    mine = make_conversation(user, title="My excursion notes")
    make_conversation(other_user, title="Someone else's secret lot")
    client.force_login(user)

    response = client.get(reverse("chat-home"))

    html = response.content.decode()
    assert "My excursion notes" in html
    assert "Someone else's secret lot" not in html
    assert f'data-conversation-item="{mine.pk}"' in html


def test_chat_home_renders_thread_for_selected_conversation(client, user):
    """``?c=<pk>`` renders the message thread with role and payload hooks."""
    conversation = make_conversation(user, title="wafer Q&A")
    add_message(conversation, role=Message.Role.USER, content="Is there an edge ring on lot 42?")
    add_message(
        conversation,
        role=Message.Role.ASSISTANT,
        content="**Yes** -- edge ring detected.",
        sources=[{"kind": "doc", "title": "Spec", "snippet": "ring rules", "score": 0.9}],
        tool_blocks=[{"type": "text", "title": "Echo", "summary": "ok"}],
    )
    client.force_login(user)

    response = client.get(f"{reverse('chat-home')}?c={conversation.pk}")

    html = response.content.decode()
    assert response.status_code == 200
    assert f'data-conversation-id="{conversation.pk}"' in html
    for message in conversation.messages.all():
        assert f'data-message-id="{message.pk}"' in html
        assert f'data-role="{message.role}"' in html
        assert f'id="msg-{message.pk}-content"' in html
        assert f'id="msg-{message.pk}-sources"' in html
        assert f'id="msg-{message.pk}-blocks"' in html


def test_foreign_conversation_is_404(client, user, other_user):
    """Another user's conversation is unreachable, not readable."""
    foreign = make_conversation(other_user, title="not yours")
    client.force_login(user)

    response = client.get(f"{reverse('chat-home')}?c={foreign.pk}")

    assert response.status_code == 404


def test_malformed_conversation_selector_is_404(client, user):
    client.force_login(user)

    response = client.get(f"{reverse('chat-home')}?c=not-a-number")

    assert response.status_code == 404


def test_sources_payload_embeds_citation_fields(client, user):
    """Citation chips hydrate from the persisted sources JSON."""
    conversation = make_conversation(user)
    add_message(
        conversation,
        role=Message.Role.ASSISTANT,
        content="Grounded answer",
        sources=[
            {
                "kind": "doc",
                "document_id": 3,
                "chunk_id": 7,
                "title": "yield_handbook",
                "snippet": "edge ring correlates with high Injection B rates",
                "score": 0.82,
            }
        ],
    )
    client.force_login(user)

    response = client.get(f"{reverse('chat-home')}?c={conversation.pk}")

    html = response.content.decode()
    last_pk = conversation.messages.last().pk
    payload = json.loads(_json_script_payload(html, f"msg-{last_pk}-sources"))
    assert payload[0]["title"] == "yield_handbook"
    assert payload[0]["score"] == 0.82
    assert "edge ring correlates" in payload[0]["snippet"]


def test_tool_blocks_payload_embeds_wafer_fields(client, user):
    """Tool cards hydrate from the persisted tool_blocks JSON (contract blocks)."""
    conversation = make_conversation(user)
    block = {
        "type": "wafer_map",
        "title": "Wafer map analysis",
        "summary": "90/100 die pass (yield 90.0%); patterns: edge_ring",
        "total_dies": 100,
        "pass_count": 90,
        "fail_count": 10,
        "yield_pct": 0.9,
        "bin_counts": {"1": 90, "2": 10},
        "edge_ring_score": 0.31,
        "center_hotspot_score": None,
        "patterns": ["edge_ring"],
        "issues": [],
    }
    add_message(conversation, role=Message.Role.ASSISTANT, content="done", tool_blocks=[block])
    client.force_login(user)

    response = client.get(f"{reverse('chat-home')}?c={conversation.pk}")

    html = response.content.decode()
    last_pk = conversation.messages.last().pk
    payload = json.loads(_json_script_payload(html, f"msg-{last_pk}-blocks"))
    assert payload[0]["type"] == "wafer_map"
    assert payload[0]["yield_pct"] == 0.9
    assert payload[0]["bin_counts"] == {"1": 90, "2": 10}
    assert payload[0]["edge_ring_score"] == 0.31


def _json_script_payload(html: str, element_id: str) -> str:
    """Extract the JSON body of a ``json_script`` element by its id."""
    marker = f'id="{element_id}"'
    start = html.index(">", html.index(marker)) + 1
    end = html.index("</script>", start)
    return html[start:end]


def test_new_conversation_creates_row_and_redirects(client, user):
    client.force_login(user)

    response = client.post(reverse("chat-new"), data={"title": "Morning triage"})

    assert response.status_code == 302
    conversation = Conversation.objects.for_user(user).get()
    assert conversation.title == "Morning triage"
    assert response["Location"] == f"{reverse('chat-home')}?c={conversation.pk}"


def test_new_conversation_defaults_title_and_requires_post(client, user):
    client.force_login(user)

    response = client.post(reverse("chat-new"), data={})

    conversation = Conversation.objects.for_user(user).get()
    assert conversation.title == "New conversation"
    assert response.status_code == 302

    get_response = client.get(reverse("chat-new"))
    assert get_response.status_code == 405  # require_POST


def test_new_conversation_requires_login(client, db):
    response = client.post(reverse("chat-new"), data={})

    assert response.status_code == 302
    assert "/dashboard/accounts/login/" in response["Location"]
    assert Conversation.objects.count() == 0


def test_delete_conversation_cascades_messages(client, user):
    conversation = make_conversation(user, title="doomed")
    add_message(conversation, role=Message.Role.USER, content="hello")
    client.force_login(user)

    response = client.post(reverse("chat-delete", args=[conversation.pk]))

    assert response.status_code == 302
    assert response["Location"] == reverse("chat-home")
    assert not Conversation.objects.filter(pk=conversation.pk).exists()
    assert Message.objects.count() == 0


def test_delete_foreign_conversation_is_404(client, user, other_user):
    foreign = make_conversation(other_user, title="not yours")
    client.force_login(user)

    response = client.post(reverse("chat-delete", args=[foreign.pk]))

    assert response.status_code == 404
    assert Conversation.objects.filter(pk=foreign.pk).exists()


def test_conversations_order_newest_first(client, user):
    """Sidebar follows the model ordering: most recently updated first."""
    older = make_conversation(user, title="older")
    newer = make_conversation(user, title="newer")
    # auto_now timestamps can land in the same microsecond; pin them.
    Conversation.objects.filter(pk=older.pk).update(updated_at=timezone.now())
    Conversation.objects.filter(pk=newer.pk).update(
        updated_at=timezone.now() + timedelta(minutes=1)
    )
    client.force_login(user)

    response = client.get(reverse("chat-home"))

    order = [item["conversation"].pk for item in response.context["conversation_items"]]
    assert order == [newer.pk, older.pk]


def test_llm_unconfigured_degraded_state(client, user):
    """Zero-key boot renders the honest degraded banner and flag."""
    client.force_login(user)

    response = client.get(reverse("chat-home"))

    html = response.content.decode()
    assert 'data-llm-configured="false"' in html
    assert "data-degraded-banner" in html
    assert "Model not configured" in html


@override_settings(OPENAI_API_KEY="test-key-not-real")
def test_llm_configured_state_has_no_banner(client, user):
    client.force_login(user)

    response = client.get(reverse("chat-home"))

    html = response.content.decode()
    assert 'data-llm-configured="true"' in html
    assert "data-degraded-banner" not in html


def test_agent_home_redirects_to_chat(client, user):
    """The legacy demo page URL now leads to the chat UI."""
    client.force_login(user)

    response = client.get(reverse("agent-home"))

    assert response.status_code == 302
    assert response["Location"] == reverse("chat-home")


def test_thread_empty_state_for_new_conversation(client, user):
    conversation = make_conversation(user, title="fresh")
    client.force_login(user)

    response = client.get(f"{reverse('chat-home')}?c={conversation.pk}")

    html = response.content.decode()
    assert "data-thread-empty" in html
    assert "Start the conversation" in html


# --- Tutor mode (spec #7): composer toggle, sidebar badge, mode endpoint ---


def _composer_toggle_state(html: str) -> str:
    """The rendered aria-pressed value of the single tutor toggle."""
    match = re.search(r'id="tutor-toggle"[^>]*aria-pressed="([^"]+)"', html)
    assert match, "the tutor toggle must render with an aria-pressed state"
    return match.group(1)


def _sidebar_item_after_link(html: str, title: str) -> str:
    """The conversation-item markup following the sidebar link for ``title``."""
    marker = f">{title}</a>"
    start = html.index(marker) + len(marker)
    return html[start : html.index("</div>", start)]


def make_tutor_conversation(user: User, title: str = "tutor conv") -> Conversation:
    conversation = make_conversation(user, title=title)
    Conversation.objects.filter(pk=conversation.pk).update(mode=Conversation.Mode.TUTOR)
    conversation.refresh_from_db()
    return conversation


def test_composer_toggle_reflects_active_tutor_conversation(client, user):
    conversation = make_tutor_conversation(user, title="Litho office hours")
    client.force_login(user)

    response = client.get(f"{reverse('chat-home')}?c={conversation.pk}")

    html = response.content.decode()
    assert _composer_toggle_state(html) == "true"
    assert 'class="tutor-toggle on"' in html


def test_composer_toggle_defaults_off_without_tutor_mode(client, user):
    make_conversation(user)
    client.force_login(user)

    response = client.get(reverse("chat-home"))

    html = response.content.decode()
    assert _composer_toggle_state(html) == "false"
    assert "tutor-toggle on" not in html


def test_tutor_conversations_are_badged_in_sidebar(client, user):
    make_tutor_conversation(user, title="Litho office hours")
    make_conversation(user, title="Regular chat")
    client.force_login(user)

    response = client.get(reverse("chat-home"))

    html = response.content.decode()
    assert "mode-badge" in _sidebar_item_after_link(html, "Litho office hours")
    assert "mode-badge" not in _sidebar_item_after_link(html, "Regular chat")


def test_set_conversation_mode_persists_and_answers_json(client, user):
    conversation = make_conversation(user)
    assert conversation.mode == Conversation.Mode.ASSISTANT  # model default
    client.force_login(user)

    response = client.post(
        reverse("chat-mode"),
        data={"conversation_id": conversation.pk, "mode": "tutor"},
    )

    assert response.status_code == 200
    assert response.json() == {"conversation_id": conversation.pk, "mode": "tutor"}
    conversation.refresh_from_db()
    assert conversation.mode == Conversation.Mode.TUTOR

    back = client.post(
        reverse("chat-mode"),
        data={"conversation_id": conversation.pk, "mode": "assistant"},
    )
    assert back.status_code == 200
    conversation.refresh_from_db()
    assert conversation.mode == Conversation.Mode.ASSISTANT


def test_set_mode_foreign_conversation_is_404(client, user, other_user):
    foreign = make_conversation(other_user, title="not yours")
    client.force_login(user)

    response = client.post(
        reverse("chat-mode"),
        data={"conversation_id": foreign.pk, "mode": "tutor"},
    )

    assert response.status_code == 404
    foreign.refresh_from_db()
    assert foreign.mode == Conversation.Mode.ASSISTANT


def test_set_mode_malformed_conversation_id_is_404(client, user):
    client.force_login(user)

    response = client.post(
        reverse("chat-mode"),
        data={"conversation_id": "not-a-number", "mode": "tutor"},
    )

    assert response.status_code == 404


def test_set_mode_unknown_value_is_400(client, user):
    conversation = make_conversation(user)
    client.force_login(user)

    response = client.post(
        reverse("chat-mode"),
        data={"conversation_id": conversation.pk, "mode": "banana"},
    )

    assert response.status_code == 400
    conversation.refresh_from_db()
    assert conversation.mode == Conversation.Mode.ASSISTANT


def test_set_mode_requires_login(client, db):
    # login_required wraps require_POST, so anonymous requests redirect
    # before the method check ever runs.
    post_response = client.post(
        reverse("chat-mode"), data={"conversation_id": "1", "mode": "tutor"}
    )
    assert post_response.status_code == 302
    assert "/dashboard/accounts/login/" in post_response["Location"]
    assert Conversation.objects.count() == 0


def test_set_mode_requires_post(client, user):
    client.force_login(user)

    get_response = client.get(reverse("chat-mode"))

    assert get_response.status_code == 405  # require_POST
