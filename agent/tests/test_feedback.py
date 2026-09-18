"""Per-message feedback: model uniqueness and endpoint isolation/behavior.

Zero-network: pure ORM + Django test client. The endpoint is the trust
loop's write path -- one row per (user, message), foreign messages 404,
and every invalid input answers 400 rather than silently ignoring.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.db import IntegrityError
from django.urls import reverse

from assistant.models import Conversation, Message, MessageFeedback

pytestmark = pytest.mark.django_db


@pytest.fixture
def user() -> User:
    return User.objects.create_user(username="feedback-user", password="pw-123456")


@pytest.fixture
def other_user() -> User:
    return User.objects.create_user(username="feedback-other", password="pw-123456")


@pytest.fixture
def assistant_message(user: User) -> Message:
    conversation = Conversation.objects.create(user=user, title="wafer Q&A")
    return Message.objects.create(
        conversation=conversation,
        role=Message.Role.ASSISTANT,
        content="**Yes** -- edge ring detected.",
    )


def post_feedback(client, message_pk: int, value: str, **extra):
    return client.post(
        reverse("message-feedback"),
        {"message_id": message_pk, "value": value, **extra},
    )


def test_anonymous_is_redirected_to_login(client, assistant_message):
    response = post_feedback(client, assistant_message.pk, "up")

    assert response.status_code == 302
    assert "/dashboard/accounts/login/" in response["Location"]


def test_up_feedback_creates_one_row(client, user, assistant_message):
    client.force_login(user)

    response = post_feedback(client, assistant_message.pk, "up")

    assert response.status_code == 200
    assert response.json() == {"message_id": assistant_message.pk, "value": "up"}
    feedback = MessageFeedback.objects.get(message=assistant_message, user=user)
    assert feedback.value == "up"


def test_resubmission_updates_the_same_row(client, user, assistant_message):
    """A changed mind upserts -- the uniqueness contract, exercised by the UI."""
    client.force_login(user)

    first = post_feedback(client, assistant_message.pk, "up")
    second = post_feedback(client, assistant_message.pk, "down")

    assert first.status_code == second.status_code == 200
    assert MessageFeedback.objects.filter(message=assistant_message, user=user).count() == 1
    assert MessageFeedback.objects.get(message=assistant_message, user=user).value == "down"


def test_unique_constraint_per_user_message(user, assistant_message):
    """Direct-ORM duplicates are impossible: one row per user+message."""
    MessageFeedback.objects.create(message=assistant_message, user=user, value="up")

    with pytest.raises(IntegrityError):
        MessageFeedback.objects.create(message=assistant_message, user=user, value="down")


def test_same_message_can_hold_feedback_from_two_users(user, other_user, assistant_message):
    """Uniqueness is per (user, message), not per message."""
    MessageFeedback.objects.create(message=assistant_message, user=user, value="up")

    second = MessageFeedback.objects.create(
        message=assistant_message, user=other_user, value="down"
    )

    assert MessageFeedback.objects.filter(message=assistant_message).count() == 2
    assert second.value == "down"


def test_clear_removes_the_row(client, user, assistant_message):
    client.force_login(user)
    post_feedback(client, assistant_message.pk, "up")

    response = post_feedback(client, assistant_message.pk, "none")

    assert response.status_code == 200
    assert response.json() == {"message_id": assistant_message.pk, "value": None}
    assert MessageFeedback.objects.filter(message=assistant_message, user=user).count() == 0


def test_clear_without_existing_feedback_is_idempotent(client, user, assistant_message):
    client.force_login(user)

    response = post_feedback(client, assistant_message.pk, "none")

    assert response.status_code == 200
    assert MessageFeedback.objects.count() == 0


def test_foreign_message_is_404(client, user, other_user):
    """Another user's conversation is unreachable, not readable."""
    foreign_conversation = Conversation.objects.create(user=other_user, title="not yours")
    foreign_message = Message.objects.create(
        conversation=foreign_conversation, role=Message.Role.ASSISTANT, content="secret answer"
    )
    client.force_login(user)

    response = post_feedback(client, foreign_message.pk, "up")

    assert response.status_code == 404
    assert MessageFeedback.objects.count() == 0


def test_user_message_feedback_is_400(client, user):
    conversation = Conversation.objects.create(user=user)
    user_message = Message.objects.create(
        conversation=conversation, role=Message.Role.USER, content="my question"
    )
    client.force_login(user)

    response = post_feedback(client, user_message.pk, "up")

    assert response.status_code == 400
    assert MessageFeedback.objects.count() == 0


def test_unknown_value_is_400(client, user, assistant_message):
    client.force_login(user)

    response = post_feedback(client, assistant_message.pk, "meh")

    assert response.status_code == 400
    assert MessageFeedback.objects.count() == 0


def test_missing_message_id_is_404(client, user):
    client.force_login(user)

    response = client.post(reverse("message-feedback"), {"value": "up"})

    assert response.status_code == 404


def test_optional_note_is_persisted(client, user, assistant_message):
    client.force_login(user)

    response = post_feedback(client, assistant_message.pk, "down", note="missed the edge ring")

    assert response.status_code == 200
    feedback = MessageFeedback.objects.get(message=assistant_message, user=user)
    assert feedback.note == "missed the edge ring"


def test_value_update_without_note_keeps_the_stored_note(client, user, assistant_message):
    """An absent note field means unchanged -- only an explicit note rewrites it."""
    client.force_login(user)
    post_feedback(client, assistant_message.pk, "down", note="check the ring")

    post_feedback(client, assistant_message.pk, "up")

    feedback = MessageFeedback.objects.get(message=assistant_message, user=user)
    assert feedback.value == "up"
    assert feedback.note == "check the ring"
