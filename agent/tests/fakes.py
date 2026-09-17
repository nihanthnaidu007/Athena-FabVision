"""Test doubles shared by the agent test suite.

Every fake here is zero-network: the FakeLLM scripts LLMEvents
directly, FakeOpenAISDK mimics the ``AsyncOpenAI`` chat surface, and
stub modules stand in for the optional ``fabtools`` / ``rag``
integration packages.
"""

from __future__ import annotations

import asyncio
import functools
import types
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

from agent.llm import LLMEvent, ToolCallRequest


def async_test(fn: Callable[..., Awaitable[None]]) -> Callable[..., None]:
    """Run an async test function under asyncio.run -- no plugin needed."""

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> None:
        asyncio.run(fn(*args, **kwargs))

    return wrapper


class FakeLLM:
    """Scripted LLMClient: one round per ``stream`` call.

    Each script entry is a list of LLMEvents; an entry may instead be an
    Exception instance, which is raised at that point in the stream
    (mid-script failures included). Records every call for assertions.
    """

    model_name = "fake-model"

    def __init__(self, rounds: list[Any]):
        self._rounds = list(rounds)
        self.calls: list[dict[str, Any]] = []

    async def stream(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[LLMEvent]:
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        for item in self._rounds.pop(0):
            if isinstance(item, Exception):
                raise item
            yield item


class ExplodingLLM(FakeLLM):
    """A client whose stream always fails before emitting anything."""

    def __init__(self, error: Exception | None = None):
        super().__init__([[]])
        self.error = error or RuntimeError("upstream connection failed")

    async def stream(self, **kwargs: Any) -> AsyncIterator[LLMEvent]:
        self.calls.append(kwargs)
        raise self.error
        yield  # pragma: no cover -- makes this an async generator


def delta(text: str) -> LLMEvent:
    return LLMEvent(type="delta", text=text)


def tool_call(name: str, arguments: str, call_id: str = "call_1") -> LLMEvent:
    return LLMEvent(
        type="tool_call", tool_call=ToolCallRequest(id=call_id, name=name, arguments=arguments)
    )


def usage(tokens_in: int = 11, tokens_out: int = 7) -> LLMEvent:
    return LLMEvent(type="usage", usage={"tokens_in": tokens_in, "tokens_out": tokens_out})


async def echo_tool(user: Any, **kwargs: Any) -> dict[str, Any]:
    """Contract-shaped fake tool: async, (user, **kwargs) -> dict block."""
    return {
        "type": "text",
        "title": "Echo",
        "summary": f"echo:{kwargs.get('value', '')}",
        "context": "echo context for the model",
    }


async def failing_tool(user: Any, **kwargs: Any) -> dict[str, Any]:
    raise ValueError("wafer file not found")


class _AsyncIterable:
    def __init__(self, items: list[Any]):
        self._items = items

    def __aiter__(self) -> _AsyncIterable:
        self._iterator = iter(self._items)
        return self

    async def __anext__(self) -> Any:
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _fragment(
    index: int, id: str | None = None, name: str | None = None, arguments: str | None = None
):
    from types import SimpleNamespace

    return SimpleNamespace(
        index=index, id=id, function=SimpleNamespace(name=name, arguments=arguments)
    )


def _chunk(content: str | None = None, tool_calls: list | None = None, usage_data: Any = None):
    from types import SimpleNamespace

    choices = []
    if content or tool_calls:
        choices.append(
            SimpleNamespace(
                delta=SimpleNamespace(content=content, tool_calls=tool_calls), finish_reason=None
            )
        )
    return SimpleNamespace(choices=choices, usage=usage_data)


class FakeOpenAISDK:
    """Mimics ``AsyncOpenAI().chat.completions.create(stream=True, ...)``
    closely enough to exercise OpenAIChatClient without any network."""

    def __init__(self, chunks: list[Any]):
        self._chunks = chunks
        self.calls: list[dict[str, Any]] = []
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))

    async def _create(self, **kwargs: Any) -> _AsyncIterable:
        self.calls.append(kwargs)
        return _AsyncIterable(self._chunks)


# A streamed completion: two text deltas, a tool call whose arguments
# arrive in two fragments, then the usage-only terminator chunk.
TOOL_CALL_SDK_CHUNKS = [
    _chunk(content="Let me check the wafer. "),
    _chunk(content="One moment."),
    _chunk(
        content=None,
        tool_calls=[_fragment(0, id="call_abc", name="wafer_map_analyze", arguments='{"path')],
    ),
    _chunk(content=None, tool_calls=[_fragment(0, name=None, arguments='": "w1.csv"}')]),
    _chunk(usage_data=types.SimpleNamespace(prompt_tokens=11, completion_tokens=7)),
]


def stub_module(name: str, **attributes: Any) -> types.ModuleType:
    """Build a module object standing in for an optional integration."""
    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def stub_importer(modules: dict[str, Any]):
    """import_module replacement failing for anything not in ``modules``."""

    def _import(name: str, *args: Any, **kwargs: Any):
        if name in modules:
            return modules[name]
        raise ImportError(f"No module named {name!r}")

    return _import
