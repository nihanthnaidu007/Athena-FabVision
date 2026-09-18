"""agent/loop.py: event sequence, tool round trip, persistence, usage.

Every test runs the real loop with a FakeLLM and fake tools -- zero
network. ORM access flows through sync_to_async inside the loop, so
tests use transactional database markers: writes from the loop's
executor thread commit immediately and are visible to test reads.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from django.contrib.auth.models import User

from agent import loop as loop_module
from agent.loop import (
    SYSTEM_PROMPT,
    TUTOR_SYSTEM_PROMPT,
    resolve_system_prompt,
    run_agent,
)
from agent.registry import ToolRegistry
from assistant.models import Chunk, Conversation, Document, Message, UsageEvent

from .fakes import FakeLLM, async_test, delta, echo_tool, failing_tool, tool_call, usage


def make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register("echo_tool", echo_tool)
    reg.register("failing_tool", failing_tool)
    return reg


@pytest.fixture
def user(transactional_db):
    return User.objects.create_user(username="fab-user", password="x")


@pytest.fixture
def conversation(transactional_db, user):
    return Conversation.objects.create(user=user)


_UNSET = object()


async def run_turn(script, user, conversation, llm=_UNSET, user_input="Analyze wafer w1", **kwargs):
    """Run one scripted turn to completion, collecting AgentEvents."""

    collected = []
    async for event in run_agent(
        user=user,
        conversation=conversation,
        user_input=user_input,
        llm=FakeLLM(script) if llm is _UNSET else llm,
        registry=kwargs.pop("registry", make_registry()),
        **kwargs,
    ):
        collected.append(event)
    return collected


def event_types(events):
    return [event.type for event in events]


@async_test
async def test_event_sequence_with_tool_round_trip(db, user, conversation):
    script = [
        [
            delta("Checking the wafer. "),
            tool_call("echo_tool", '{"value": "LOT-7"}', "call_9"),
            usage(5, 2),
        ],
        [delta("Yield looks fine."), usage(9, 6)],
    ]

    events = await run_turn(script, user, conversation)

    # Full ordered contract: status start/retrieve/generate, deltas,
    # tool_call + tool_result pair, second generation round, sources, done.
    assert event_types(events) == [
        "status",
        "status",
        "status",
        "delta",
        "tool_call",
        "tool_result",
        "status",
        "delta",
        "sources",
        "turn_saved",
        "done",
    ]
    assert [e.data["stage"] for e in events if e.type == "status"] == [
        "started",
        "retrieving",
        "generating",
        "generating",
    ]
    tool_call_event = next(e for e in events if e.type == "tool_call")
    assert tool_call_event.data == {
        "name": "echo_tool",
        "arguments": {"value": "LOT-7"},
        "call_id": "call_9",
        "request_id": "",
    }
    tool_result_event = next(e for e in events if e.type == "tool_result")
    assert tool_result_event.data["name"] == "echo_tool"
    assert tool_result_event.data["block"]["type"] == "text"
    assert tool_result_event.data["block"]["summary"] == "echo:LOT-7"
    sources_event = next(e for e in events if e.type == "sources")
    assert sources_event.data["sources"] == [
        {"kind": "tool", "tool": "echo_tool", "summary": "echo:LOT-7"},
    ]
    done_event = next(e for e in events if e.type == "done")
    assert done_event.data["conversation_id"] == conversation.pk
    assert done_event.data["tokens_in"] == 14  # 5 + 9
    assert done_event.data["tokens_out"] == 8  # 2 + 6


@async_test
async def test_tool_result_fed_back_to_model(db, user, conversation):
    script = [
        [tool_call("echo_tool", '{"value": "w1"}')],
        [delta("Done."), usage()],
    ]
    fake = FakeLLM(script)

    async def _collect():
        _ = [
            event
            async for event in run_agent(
                user=user,
                conversation=conversation,
                user_input="go",
                llm=fake,
                registry=make_registry(),
            )
        ]

    await _collect()

    second_call = fake.calls[1]
    tool_message = second_call["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["tool_call_id"] == "call_1"
    # The model sees the exact block the tool returned.
    assert json.loads(tool_message["content"])["summary"] == "echo:w1"
    assistant_message = second_call["messages"][-2]
    assert assistant_message["role"] == "assistant"
    assert assistant_message["tool_calls"][0]["function"]["name"] == "echo_tool"


def test_turn_persists_user_and_assistant_messages(transactional_db, user, conversation):
    script = [
        [delta("Checking the wafer. "), tool_call("echo_tool", '{"value": "w1"}')],
        [delta("Yield looks fine."), usage()],
    ]

    async def _scenario():
        return await run_turn(script, user, conversation)

    asyncio.run(_scenario())

    contents = list(conversation.messages.values_list("role", "content"))
    assert contents == [
        ("user", "Analyze wafer w1"),
        ("assistant", "Checking the wafer. Yield looks fine."),
    ]
    assistant = conversation.messages.get(role=Message.Role.ASSISTANT)
    assert assistant.tool_blocks[0]["type"] == "text"
    assert assistant.sources == [
        {"kind": "tool", "tool": "echo_tool", "summary": "echo:w1"},
    ]


def test_usage_recorded_with_tokens_latency_and_fks(transactional_db, user, conversation):
    script = [[delta("Answer."), usage(123, 45)]]

    async def _scenario():
        return await run_turn(script, user, conversation)

    asyncio.run(_scenario())

    event = UsageEvent.objects.get()
    assert event.kind == "chat"
    assert event.user == user
    assert event.model == "fake-model"
    assert event.tokens_in == 123
    assert event.tokens_out == 45
    assert event.latency_ms >= 0
    assert event.conversation == conversation
    assert event.message == conversation.messages.get(role=Message.Role.ASSISTANT)


def test_retrieval_sources_flow_to_event_and_persisted_message(
    transactional_db, user, conversation
):
    def retrieve(user, query, k=5, notebook_id=None):
        return [
            {
                "document_id": 3,
                "chunk_id": 9,
                "title": "Runbook",
                "snippet": "edge ring fix",
                "score": 0.91,
            },
            {
                "document_id": 4,
                "chunk_id": 11,
                "title": "SOP",
                "snippet": "sort bin 3",
                "score": 0.42,
            },
        ]

    async def _scenario():
        return await run_turn(
            [[delta("Cited answer."), usage()]], user, conversation, retrieve=retrieve
        )

    events = asyncio.run(_scenario())

    sources_event = next(e for e in events if e.type == "sources")
    assert sources_event.data["sources"][0] == {
        "kind": "doc",
        "document_id": 3,
        "chunk_id": 9,
        "title": "Runbook",
        "snippet": "edge ring fix",
        "score": 0.91,
    }
    assistant = conversation.messages.get(role=Message.Role.ASSISTANT)
    assert len(assistant.sources) == 2
    assert assistant.sources[0]["kind"] == "doc"


@async_test
async def test_async_retrieval_source_supported(db, user, conversation):
    async def retrieve(user, query, k=5, notebook_id=None):
        return [
            {"document_id": 1, "chunk_id": 2, "title": "Async doc", "snippet": "s", "score": 0.5}
        ]

    events = await run_turn([[delta("A."), usage()]], user, conversation, retrieve=retrieve)
    sources_event = next(e for e in events if e.type == "sources")
    assert sources_event.data["sources"][0]["title"] == "Async doc"


@async_test
async def test_retrieval_context_reaches_model_prompt(db, user, conversation):
    def retrieve(user, query, k=5, notebook_id=None):
        return [
            {
                "document_id": 3,
                "chunk_id": 9,
                "title": "Runbook",
                "snippet": "edge ring fix",
                "score": 0.9,
            }
        ]

    fake = FakeLLM([[delta("ok."), usage()]])

    async def _collect():
        _ = [
            event
            async for event in run_agent(
                user=user,
                conversation=conversation,
                user_input="q",
                llm=fake,
                registry=make_registry(),
                retrieve=retrieve,
            )
        ]

    await _collect()
    prompt_messages = fake.calls[0]["messages"]
    context_note = prompt_messages[-2]
    assert context_note["role"] == "system"
    assert "[Runbook] edge ring fix" in context_note["content"]


@async_test
async def test_retrieval_failure_degrades_to_empty_context(db, user, conversation):
    def broken_retrieve(user, query, k=5, notebook_id=None):
        raise RuntimeError("embedding service down")

    events = await run_turn(
        [[delta("Answer without context."), usage()]],
        user,
        conversation,
        retrieve=broken_retrieve,
    )

    assert event_types(events)[-3:] == ["sources", "turn_saved", "done"]
    assert next(e for e in events if e.type == "sources").data["sources"] == []


@async_test
async def test_notebook_id_is_forwarded_to_retrieval_source(db, user, conversation):
    captured = {}

    def retrieve(user, query, k=5, notebook_id=None):
        captured["notebook_id"] = notebook_id
        return []

    await run_turn(
        [[delta("ok."), usage()]], user, conversation, retrieve=retrieve, notebook_id=17
    )

    assert captured["notebook_id"] == 17


@async_test
async def test_retrieval_without_notebook_receives_none_whole_kb(db, user, conversation):
    """Regression pin: an unscoped turn retrieves exactly as before v1.1."""
    captured = {}

    def retrieve(user, query, k=5, notebook_id=None):
        captured["notebook_id"] = notebook_id
        return []

    await run_turn([[delta("ok."), usage()]], user, conversation, retrieve=retrieve)

    assert captured["notebook_id"] is None


def test_sync_retrieval_source_runs_offloaded_from_the_event_loop(
    transactional_db, user, conversation, monkeypatch
):
    """Regression: the wired sync retrieve must run off the event loop.

    The registry wires the plain sync ``rag.retrieval.retrieve``; called
    inline inside the loop it raised SynchronousOnlyOperation under ASGI,
    which the loop swallowed -- every streamed answer degraded to "no
    document context" in the browser. The real sync source must be
    off-loaded to a thread and its sources surfaced.
    """
    from rag import retrieval as rag_retrieval
    from rag.tests.fakes import FakeEmbedder

    document = Document.objects.create(
        user=user,
        original_filename='runbook.txt',
        file_type='txt',
        sha256='0' * 64,
        status=Document.Status.READY,
    )
    embedder = FakeEmbedder()
    Chunk.objects.create(
        document=document,
        index=0,
        content='wafer yield summary',
        content_hash='chunk-0',
        embedding=embedder.embed(['wafer yield summary'])[0],
    )
    monkeypatch.setattr(rag_retrieval, 'default_embedder', lambda: embedder)

    async def _scenario():
        return await run_turn(
            [[delta("Cited answer."), usage()]],
            user,
            conversation,
            retrieve=rag_retrieval.retrieve,
            user_input='wafer',
        )

    events = asyncio.run(_scenario())

    sources_event = next(e for e in events if e.type == "sources")
    assert [source["title"] for source in sources_event.data["sources"]] == ["runbook.txt"]
    assistant = conversation.messages.get(role=Message.Role.ASSISTANT)
    assert len(assistant.sources) == 1


def test_history_is_sent_oldest_first_before_new_turn(transactional_db, user, conversation):
    Message.objects.create(
        conversation=conversation, role=Message.Role.USER, content="older question"
    )
    Message.objects.create(
        conversation=conversation, role=Message.Role.ASSISTANT, content="older answer"
    )
    Message.objects.create(
        conversation=conversation, role=Message.Role.SYSTEM, content="legacy system row"
    )
    fake = FakeLLM([[delta("ok."), usage()]])

    async def _scenario():
        return await run_turn([[]], user, conversation, llm=fake, user_input="new question")

    asyncio.run(_scenario())
    sent = fake.calls[0]["messages"]
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert sent[0]["content"].startswith("You are Athena")
    assert sent[-1]["content"] == "new question"


def test_history_is_bounded(transactional_db, user, conversation):
    for i in range(35):
        Message.objects.create(
            conversation=conversation, role=Message.Role.USER, content=f"old-{i}"
        )
    fake = FakeLLM([[delta("ok."), usage()]])

    async def _scenario():
        return await run_turn([[]], user, conversation, llm=fake)

    asyncio.run(_scenario())
    sent = fake.calls[0]["messages"]
    # system + 30 history + the new user turn
    assert len(sent) == 32
    assert sent[1]["content"] == "old-5"  # oldest of the last 30
    assert sent[-2]["content"] == "old-34"


def test_degraded_no_key_mode(transactional_db, user, conversation, monkeypatch):
    monkeypatch.setattr(loop_module, "default_client", lambda: None)

    async def _scenario():
        return await run_turn([[]], user, conversation, llm=None)

    events = asyncio.run(_scenario())

    assert event_types(events) == ["status", "error"]
    error_event = events[-1]
    assert error_event.data["code"] == "llm_unavailable"
    # The user's question is kept; nothing else is recorded.
    assert list(conversation.messages.values_list("role", flat=True)) == ["user"]
    assert UsageEvent.objects.count() == 0


def test_llm_failure_mid_stream_emits_error_and_keeps_partial(transactional_db, user, conversation):
    script = [[delta("Partial answer "), RuntimeError("boom")]]

    async def _scenario():
        return await run_turn(script, user, conversation)

    events = asyncio.run(_scenario())

    assert event_types(events)[-1] == "error"
    assert "boom" in events[-1].data["error"]
    # The stream survives; the partial text is still persisted.
    assistant = conversation.messages.get(role=Message.Role.ASSISTANT)
    assert assistant.content == "Partial answer "
    assert UsageEvent.objects.count() == 1


def test_llm_failure_before_output_persists_nothing_extra(transactional_db, user, conversation):
    class ExplodingLLM:
        model_name = "explode"

        async def stream(self, **kwargs):
            raise RuntimeError("upstream connection failed")
            yield  # pragma: no cover -- makes this an async generator

    async def _scenario():
        return await run_turn([[]], user, conversation, llm=ExplodingLLM())

    events = asyncio.run(_scenario())

    assert event_types(events)[-1] == "error"
    assert list(conversation.messages.values_list("role", flat=True)) == ["user"]
    assert UsageEvent.objects.count() == 0


def test_tool_failure_block_fed_back_and_stream_continues(transactional_db, user, conversation):
    script = [
        [tool_call("failing_tool", "{}")],
        [delta("The tool failed; here is what I know."), usage(3, 3)],
    ]

    async def _scenario():
        return await run_turn(script, user, conversation)

    events = asyncio.run(_scenario())

    tool_result_event = next(e for e in events if e.type == "tool_result")
    assert tool_result_event.data["block"]["type"] == "error"
    assert tool_result_event.data["block"]["error"] == "wafer file not found"
    # The turn completes normally after the tool error.
    assert event_types(events)[-1] == "done"
    assistant = conversation.messages.get(role=Message.Role.ASSISTANT)
    assert assistant.tool_blocks[0]["type"] == "error"
    assert assistant.content.endswith("here is what I know.")


def test_tool_rounds_are_bounded(transactional_db, user, conversation):
    call = tool_call("echo_tool", "{}")
    script = [[call], [call], [call]]  # would loop forever unbounded

    async def _scenario():
        return await run_turn(script, user, conversation, max_tool_rounds=2)

    events = asyncio.run(_scenario())

    assert event_types(events).count("tool_call") == 2
    assert event_types(events)[-1] == "done"


@async_test
async def test_request_id_flows_into_events(db, user, conversation):
    events = await run_turn([[delta("hi."), usage()]], user, conversation, request_id="req-123")

    for event in events:
        assert event.data["request_id"] == "req-123"


def test_abort_preserves_partial_text(transactional_db, user, conversation):
    script = [[delta("Checking the wafer. "), delta("Almost there."), usage(50, 5)]]

    async def _scenario():
        agen = run_agent(
            user=user,
            conversation=conversation,
            user_input="Analyze wafer w1",
            llm=FakeLLM(script),
            registry=make_registry(),
        )
        async for event in agen:
            if event.type == "delta":
                await agen.aclose()  # client disconnect
                break

    asyncio.run(_scenario())

    assistant = conversation.messages.get(role=Message.Role.ASSISTANT)
    assert assistant.content == "Checking the wafer. "
    assert UsageEvent.objects.count() == 1
    # The disconnect lands before the LLM emits its usage event, so the
    # recorded tokens are honestly zero -- latency is still real.
    assert UsageEvent.objects.get().tokens_in == 0


def test_system_prompt_is_fab_domain_grounded():
    assert "semiconductor" in SYSTEM_PROMPT
    assert "cite" in SYSTEM_PROMPT


def test_tutor_preset_tutors_and_keeps_grounding():
    assert "semiconductor" in TUTOR_SYSTEM_PROMPT
    assert "cite" in TUTOR_SYSTEM_PROMPT
    assert "Never give the final answer" in TUTOR_SYSTEM_PROMPT
    assert "guiding" in TUTOR_SYSTEM_PROMPT


def test_resolve_system_prompt_swaps_only_for_tutor_mode():
    assert resolve_system_prompt(Conversation.Mode.ASSISTANT) is SYSTEM_PROMPT
    assert resolve_system_prompt(Conversation.Mode.TUTOR) is TUTOR_SYSTEM_PROMPT
    # Unknown or blank values degrade to the default assistant behavior.
    assert resolve_system_prompt("") is SYSTEM_PROMPT
    assert resolve_system_prompt("garbage") is SYSTEM_PROMPT


@async_test
async def test_tutor_conversation_builds_prompt_from_tutor_preset(db, user, conversation):
    conversation.mode = Conversation.Mode.TUTOR
    fake = FakeLLM([[delta("Hint: compare the edge dies first."), usage()]])

    async def _collect():
        _ = [
            event
            async for event in run_agent(
                user=user,
                conversation=conversation,
                user_input="Why is yield low?",
                llm=fake,
                registry=make_registry(),
            )
        ]

    await _collect()

    prompt_messages = fake.calls[0]["messages"]
    assert prompt_messages[0]["role"] == "system"
    assert prompt_messages[0]["content"] == TUTOR_SYSTEM_PROMPT
    assert prompt_messages[0]["content"] != SYSTEM_PROMPT


@async_test
async def test_assistant_conversation_keeps_default_preset(db, user, conversation):
    fake = FakeLLM([[delta("Answer."), usage()]])

    async def _collect():
        _ = [
            event
            async for event in run_agent(
                user=user,
                conversation=conversation,
                user_input="Why is yield low?",
                llm=fake,
                registry=make_registry(),
            )
        ]

    await _collect()

    assert fake.calls[0]["messages"][0]["content"] == SYSTEM_PROMPT


def test_tutor_turn_persists_like_any_turn(transactional_db, user, conversation):
    """Tutor mode swaps the preset, not the loop contract."""
    conversation.mode = Conversation.Mode.TUTOR

    asyncio.run(run_turn([[delta("Hint: check the edge ring."), usage()]], user, conversation))

    contents = list(conversation.messages.values_list("role", "content"))
    assert contents == [
        ("user", "Analyze wafer w1"),
        ("assistant", "Hint: check the edge ring."),
    ]
    assert UsageEvent.objects.count() == 1


def test_turn_saved_event_carries_assistant_message_id(user, conversation):
    """The turn_saved event exposes the persisted assistant pk (feedback hook)."""

    async def _scenario():
        return await run_turn([[delta("Hello.")]], user, conversation)

    events = asyncio.run(_scenario())

    saved = [event for event in events if event.type == "turn_saved"]
    assert len(saved) == 1
    assistant = conversation.messages.get(role=Message.Role.ASSISTANT)
    assert saved[0].data["message_id"] == assistant.pk
    # done still terminates the stream, after the save notice.
    assert event_types(events)[-1] == "done"


def test_failed_turn_persists_partial_without_turn_saved(user, conversation):
    """A turn that fails mid-stream keeps its partial answer but never
    emits turn_saved: the client has no pk to hang feedback on, which is
    honest -- there is no complete answer to rate."""

    async def _scenario():
        script = [[delta("Partial answer "), RuntimeError("boom")]]
        return await run_turn(script, user, conversation)

    events = asyncio.run(_scenario())

    assert not [event for event in events if event.type == "turn_saved"]
    assert conversation.messages.get(role=Message.Role.ASSISTANT).content == "Partial answer "
