"""LLM clients behind an injectable interface.

The agent loop consumes the :class:`LLMClient` protocol only, so tests
inject a fake client and never touch the network. The OpenAI wrapper is
the only place the real SDK is imported (lazily, inside the class --
keeping the module importable with no API key configured).

Zero-key boot contract: without ``OPENAI_API_KEY`` the module imports
cleanly, :func:`llm_available` is False, and :func:`default_client`
returns None -- callers degrade to an ``llm_unavailable`` error event
instead of crashing.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from django.conf import settings

logger = logging.getLogger(__name__)

DEFAULT_CHAT_MODEL = 'gpt-4o-mini'


@dataclass(frozen=True)
class ToolCallRequest:
    """One tool invocation requested by the model, arguments still JSON."""

    id: str
    name: str
    arguments: str  # raw JSON string; may stream in across chunks


@dataclass(frozen=True)
class LLMEvent:
    """Typed event yielded by a streaming LLM client.

    ``delta`` carries the next text fragment; ``tool_call`` a completed
    tool request (flushed when the stream ends); ``usage`` the token
    counts of the finished call.
    """

    type: str  # delta | tool_call | usage
    text: str = ''
    tool_call: ToolCallRequest | None = None
    usage: dict[str, int] = field(default_factory=dict)


class LLMClient(Protocol):
    """The surface the agent loop programs against; fakes implement this."""

    model_name: str

    def stream(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[LLMEvent]:
        """Yield delta/tool_call/usage events for one completion round."""
        ...


def llm_available() -> bool:
    """True when an OpenAI key is configured (read live, not at import)."""
    return bool(getattr(settings, 'OPENAI_API_KEY', None))


class OpenAIChatClient:
    """Streaming chat client over the openai SDK.

    ``sdk`` accepts an ``AsyncOpenAI``-shaped object so tests can inject
    a fake with the same ``chat.completions.create`` surface and no
    network. Without it, a real client is built from the settings key.
    """

    def __init__(self, *, api_key: str | None = None, model: str | None = None, sdk: Any = None):
        self.model_name = model or getattr(settings, 'OPENAI_CHAT_MODEL', DEFAULT_CHAT_MODEL)
        if sdk is not None:
            self._sdk = sdk
        else:
            import openai  # imported lazily: no network, but never at module import

            key = api_key or getattr(settings, 'OPENAI_API_KEY', None)
            self._sdk = openai.AsyncOpenAI(api_key=key)

    async def stream(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> AsyncIterator[LLMEvent]:
        """Stream one completion round as typed events.

        Tool-call argument fragments accumulate across chunks and are
        flushed as one ``tool_call`` event per request when the stream
        ends. The caller (agent loop) re-invokes ``stream`` with the tool
        results appended for the next round.
        """
        kwargs: dict[str, Any] = {
            'model': self.model_name,
            'messages': messages,
            'stream': True,
            'stream_options': {'include_usage': True},
        }
        if tools:
            kwargs['tools'] = tools
        response = await self._sdk.chat.completions.create(**kwargs)

        pending: dict[int, dict[str, str]] = {}
        async for chunk in response:
            for choice in chunk.choices or ():
                delta = choice.delta
                if delta.content:
                    yield LLMEvent(type='delta', text=delta.content)
                for fragment in getattr(delta, 'tool_calls', None) or ():
                    slot = pending.setdefault(
                        fragment.index, {'id': '', 'name': '', 'arguments': ''}
                    )
                    if fragment.id:
                        slot['id'] = fragment.id
                    function = getattr(fragment, 'function', None)
                    if function is None:
                        continue
                    if function.name:
                        slot['name'] += function.name
                    if function.arguments:
                        slot['arguments'] += function.arguments
            usage = getattr(chunk, 'usage', None)
            if usage is not None:
                yield LLMEvent(
                    type='usage',
                    usage={
                        'tokens_in': usage.prompt_tokens or 0,
                        'tokens_out': usage.completion_tokens or 0,
                    },
                )
        for index in sorted(pending):
            slot = pending[index]
            if not slot['name']:
                logger.warning('Discarding a tool-call fragment with no function name.')
                continue
            yield LLMEvent(
                type='tool_call',
                tool_call=ToolCallRequest(
                    id=slot['id'] or f'call_{index}', name=slot['name'], arguments=slot['arguments']
                ),
            )


def default_client() -> LLMClient | None:
    """Build the settings-configured client, or None in degraded mode."""
    if not llm_available():
        return None
    return OpenAIChatClient()


def tool_result_content(block: dict[str, Any]) -> str:
    """Serialize a tool result block for the model's ``tool`` message."""
    return json.dumps(block, default=str)
