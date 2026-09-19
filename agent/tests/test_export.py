"""agent/export.py + the export view: Markdown export contract.

The export is the plumbing the RCA report generator (spec #11) reuses,
so its output is pinned here as exact snapshots: deterministic, no
timestamps, no ORM access inside the pure functions. Zero-network.
"""

from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.urls import reverse

from agent.export import (
    conversation_to_markdown,
    render_sources_markdown,
    render_tool_block_markdown,
)
from assistant.models import Conversation, Message

pytestmark = pytest.mark.django_db


@pytest.fixture
def user() -> User:
    return User.objects.create_user(username="export-user", password="pw-123456")


@pytest.fixture
def other_user() -> User:
    return User.objects.create_user(username="export-other", password="pw-123456")


# --- pure functions ----------------------------------------------------------


def test_table_block_renders_a_markdown_table():
    block = {
        "type": "table",
        "title": "Bin distribution",
        "summary": "Per-bin die counts.",
        "columns": ["bin", "dies"],
        "rows": [[1, 69], [3, 8]],
    }

    rendered = render_tool_block_markdown(block)

    assert rendered.startswith("**Bin distribution**")
    assert "Per-bin die counts." in rendered
    assert "| bin | dies |" in rendered
    assert "| --- | --- |" in rendered
    assert "| 1 | 69 |" in rendered
    assert "| 3 | 8 |" in rendered


def test_table_cells_escape_pipes_and_newlines():
    block = {"type": "table", "columns": ["note"], "rows": [["a|b\nc"]]}

    rendered = render_tool_block_markdown(block)

    assert "| a\\|b c |" in rendered


def test_wafer_block_renders_stats_patterns_and_lattice_note():
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
        "issues": ["2 dies at unknown coordinates were dropped."],
        "dies": [{"x": -4, "y": -4, "bin": 1}, {"x": -3, "y": -4, "bin": 1}],
        "dies_omitted": 0,
    }

    rendered = render_tool_block_markdown(block)

    assert "**Wafer map analysis**" in rendered
    assert "| Total dies | 100 |" in rendered
    assert "| Yield | 0.9 |" in rendered
    # A None metric (no center hotspot) is omitted, not rendered as "None".
    assert "Center hotspot" not in rendered
    assert "Patterns: edge_ring" in rendered
    assert "- 2 dies at unknown coordinates were dropped." in rendered
    assert "Die-level lattice: 2 dies in the on-screen grid." in rendered


def test_spc_block_renders_series_table_and_violations():
    block = {
        "type": "spc_chart",
        "title": "SPC control-chart check",
        "summary": "2 of 5 points violate a Nelson rule.",
        "mean": 10.0,
        "sigma": 0.5,
        "ucl": 11.5,
        "lcl": 8.5,
        "verdict": "out_of_control",
        "values": [10.0, 12.0, 10.0, 9.0, 10.0],
        "point_flags": [[], ["nelson_1"], [], [], []],
        "violations": [
            {"rule": "nelson_1", "label": "Nelson rule 1", "detail": "1 point beyond 3σ"}
        ],
        "issues": [],
        "references": ["Montgomery, Introduction to SQC"],
    }

    rendered = render_tool_block_markdown(block)

    assert "| Verdict | out_of_control |" in rendered
    assert "| 2 | 12.0 | nelson_1 |" in rendered
    assert "- Nelson rule 1 — 1 point beyond 3σ" in rendered
    assert "- Reference: Montgomery, Introduction to SQC" in rendered


def test_long_spc_series_exports_as_a_note():
    block = {
        "type": "spc_chart",
        "values": list(range(60)),
        "point_flags": [],
        "violations": [],
    }

    rendered = render_tool_block_markdown(block)

    assert "Series of 60 points omitted from export." in rendered
    assert "| Point" not in rendered


def test_error_and_text_blocks_render_inline():
    error = {"type": "error", "title": "Tool error", "detail": "wafer file not found"}
    text = {"type": "text", "title": "Echo", "summary": "echo:LOT-7"}

    assert render_tool_block_markdown(error) == "> Tool error: wafer file not found"
    assert render_tool_block_markdown(text) == "**Echo**\n\necho:LOT-7"


def test_unknown_block_type_renders_as_fenced_json():
    block = {"type": "holo_projection", "title": "Future", "alpha": 1}

    rendered = render_tool_block_markdown(block)

    assert "```json" in rendered
    assert '"type": "holo_projection"' in rendered


def test_sources_render_docs_with_snippets_and_tools():
    sources = [
        {"kind": "doc", "title": "yield_handbook", "snippet": "edge rings", "score": 0.82},
        {"kind": "tool", "tool": "wafer_map_analyze", "summary": "90/100 die pass"},
        {"kind": "doc", "title": "No score doc", "snippet": ""},
    ]

    rendered = render_sources_markdown(sources)

    assert rendered == (
        '- yield_handbook — "edge rings" — relevance 0.82\n'
        "- tool: wafer_map_analyze — 90/100 die pass\n"
        "- No score doc"
    )


def test_conversation_export_snapshot(user):
    """The full document contract: roles, content, sources, and tool blocks.

    Pinned exactly -- this is the plumbing the RCA generator reuses, so
    format drift shows up here, not in shipped reports.
    """
    conversation = Conversation.objects.create(user=user, title="Wafer Q&A")
    Message.objects.create(
        conversation=conversation,
        role=Message.Role.USER,
        content="Is there an edge ring on lot 42?",
    )
    Message.objects.create(
        conversation=conversation,
        role=Message.Role.ASSISTANT,
        content="**Yes** -- edge ring detected.",
        sources=[
            {"kind": "doc", "title": "yield_handbook", "snippet": "ring rules", "score": 0.9},
            {"kind": "tool", "tool": "wafer_map_analyze", "summary": "edge ring found"},
        ],
        tool_blocks=[
            {
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
        ],
    )
    messages = list(conversation.messages.all())

    document = conversation_to_markdown(conversation, messages)

    assert document == (
        "# Wafer Q&A\n"
        "\n"
        "## User\n"
        "\n"
        "Is there an edge ring on lot 42?\n"
        "\n"
        "## Assistant\n"
        "\n"
        "**Yes** -- edge ring detected.\n"
        "\n"
        "### Sources\n"
        "\n"
        '- yield_handbook — "ring rules" — relevance 0.9\n'
        "- tool: wafer_map_analyze — edge ring found\n"
        "\n"
        "### Tool result\n"
        "\n"
        "**Wafer map analysis**\n"
        "\n"
        "90/100 die pass (yield 90.0%); patterns: edge_ring\n"
        "\n"
        "| Metric | Value |\n"
        "| --- | --- |\n"
        "| Total dies | 100 |\n"
        "| Pass | 90 |\n"
        "| Fail | 10 |\n"
        "| Yield | 0.9 |\n"
        "| Edge-ring score | 0.31 |\n"
        "\n"
        "Patterns: edge_ring\n"
        "\n"
        "| bin 1 | bin 2 |\n"
        "| --- | --- |\n"
        "| 90 | 10 |\n"
    )


def test_empty_conversation_exports_an_honest_note(user):
    conversation = Conversation.objects.create(user=user, title="fresh")

    document = conversation_to_markdown(conversation, [])

    assert document == "# fresh\n\n_No messages yet._\n"


# --- the export view ---------------------------------------------------------


def test_export_downloads_markdown_attachment(client, user):
    conversation = Conversation.objects.create(user=user, title="Morning triage")
    Message.objects.create(conversation=conversation, role=Message.Role.USER, content="hello")
    client.force_login(user)

    response = client.get(reverse("chat-export", args=[conversation.pk]))

    assert response.status_code == 200
    assert response["Content-Type"] == "text/markdown; charset=utf-8"
    assert response["Content-Disposition"] == (
        f'attachment; filename="morning-triage-{conversation.pk}.md"'
    )
    assert response.content.decode().startswith("# Morning triage")


def test_export_is_user_scoped(client, user, other_user):
    foreign = Conversation.objects.create(user=other_user, title="not yours")
    Message.objects.create(conversation=foreign, role=Message.Role.USER, content="secret")
    client.force_login(user)

    response = client.get(reverse("chat-export", args=[foreign.pk]))

    assert response.status_code == 404
    assert b"secret" not in response.content


def test_export_requires_login(client, db):
    response = client.get(reverse("chat-export", args=[1]))

    assert response.status_code == 302
    assert "/dashboard/accounts/login/" in response["Location"]
