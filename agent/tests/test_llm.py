"""agent/llm.py: protocol, availability flag, and the OpenAI wrapper."""

from __future__ import annotations

import pytest
from django.test import override_settings

from agent import llm
from agent.llm import OpenAIChatClient

from .fakes import TOOL_CALL_SDK_CHUNKS, FakeOpenAISDK, async_test


def test_module_imports_and_degrades_without_key():
    # The pytest bootstrap runs with no OPENAI_API_KEY configured: the
    # module imports and answers honestly about availability.
    assert llm.llm_available() is False
    assert llm.default_client() is None


@override_settings(OPENAI_API_KEY="sk-test-key")
def test_available_with_key_builds_client():
    assert llm.llm_available() is True
    client = llm.default_client()
    assert isinstance(client, OpenAIChatClient)
    assert client.model_name == "gpt-4o-mini"  # settings default


@override_settings(OPENAI_API_KEY="sk-test-key", OPENAI_CHAT_MODEL="gpt-5-fab")
def test_model_name_from_settings():
    assert OpenAIChatClient().model_name == "gpt-5-fab"


@async_test
async def test_openai_client_streams_deltas_usage_and_flushes_tool_calls():
    sdk = FakeOpenAISDK(TOOL_CALL_SDK_CHUNKS)
    client = OpenAIChatClient(model="fake-model", sdk=sdk)

    events = [event async for event in client.stream(messages=[{"role": "user", "content": "hi"}])]

    types = [event.type for event in events]
    assert types == ["delta", "delta", "usage", "tool_call"]
    assert [event.text for event in events if event.type == "delta"] == [
        "Let me check the wafer. ",
        "One moment.",
    ]
    usage_event = next(event for event in events if event.type == "usage")
    assert usage_event.usage == {"tokens_in": 11, "tokens_out": 7}
    call_event = next(event for event in events if event.type == "tool_call")
    # Argument fragments arriving across chunks are reassembled exactly.
    assert call_event.tool_call.id == "call_abc"
    assert call_event.tool_call.name == "wafer_map_analyze"
    assert call_event.tool_call.arguments == '{"path": "w1.csv"}'


@async_test
async def test_openai_client_passes_model_messages_and_tools():
    sdk = FakeOpenAISDK([])
    client = OpenAIChatClient(model="fake-model", sdk=sdk)
    tools = [{"type": "function", "function": {"name": "x"}}]

    _ = [
        event
        async for event in client.stream(messages=[{"role": "user", "content": "hi"}], tools=tools)
    ]

    kwargs = sdk.calls[0]
    assert kwargs["model"] == "fake-model"
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
    assert kwargs["tools"] == tools
    assert kwargs["stream"] is True


@async_test
async def test_openai_client_propagates_sdk_errors():
    class BrokenSDK:
        class chat:
            class completions:
                @staticmethod
                async def create(**kwargs):
                    raise RuntimeError("upstream down")

    client = OpenAIChatClient(model="fake-model", sdk=BrokenSDK)
    with pytest.raises(RuntimeError, match="upstream down"):
        _ = [event async for event in client.stream(messages=[])]


def test_tool_result_content_serializes_blocks():
    assert (
        llm.tool_result_content({"type": "text", "title": "T"}) == '{"type": "text", "title": "T"}'
    )
