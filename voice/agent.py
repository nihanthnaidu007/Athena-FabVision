"""The Athena voice agent: livekit-agents 1.x wired to the shared agent core.

The voice path never reimplements agent logic. Every function tool routes
through ``agent.loop.run_agent`` or ``agent.registry`` -- the same core the
web SSE endpoint uses -- so retrieval, fab tools, persistence, and metering
behave identically across web chat and voice. The session's own LLM
(OpenAI STT/LLM/TTS via the LiveKit plugins) drives conversational flow;
these tools hand it the shared core's reasoning.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from livekit.agents import Agent, RunContext, function_tool

from agent import registry as tool_registry
from agent.loop import SYSTEM_PROMPT, run_agent

logger = logging.getLogger(__name__)

#: Spoken when a voice room has no resolvable account: tools that need a
#: user (persistence, per-user retrieval) answer honestly instead of failing.
DEGRADED_NO_ACCOUNT = (
    'Voice is running without a linked account, so I cannot run fab tools or '
    'knowledge-base retrieval. General conversation still works.'
)

RETRIEVAL_K = 5


async def run_voice_turn(
    *,
    user: Any,
    conversation: Any,
    user_input: str,
    request_id: str = '',
    llm: Any = None,
    registry: tool_registry.ToolRegistry | None = None,
    retrieve: Any = None,
) -> str:
    """One shared-core agent turn, collapsed to the final answer text.

    Consumes the same typed event stream the SSE endpoint serves: deltas
    join into the answer, and an error event becomes the answer when no
    text was produced (the loop has already persisted any partial answer).
    """
    if user is None or conversation is None:
        return DEGRADED_NO_ACCOUNT
    text_parts: list[str] = []
    error = ''
    async for event in run_agent(
        user=user,
        conversation=conversation,
        user_input=user_input,
        llm=llm,
        registry=registry,
        retrieve=retrieve,
        request_id=request_id,
    ):
        if event.type == 'delta':
            text_parts.append(str(event.data.get('text') or ''))
        elif event.type == 'error':
            error = str(event.data.get('error') or '')
    answer = ''.join(text_parts).strip()
    return answer or error or 'The turn produced no answer.'


def format_retrieval_for_voice(sources: list[dict[str, Any]]) -> str:
    """Render normalized retrieval hits as speakable, cited lines."""
    if not sources:
        return 'No knowledge-base context found for that query.'
    return '\n'.join(
        f"[{source['title']}] {source['snippet']} (relevance {source['score']:.2f})"
        for source in sources
    )


async def retrieve_for_voice(
    *,
    user: Any,
    query: str,
    registry: tool_registry.ToolRegistry | None = None,
    k: int = RETRIEVAL_K,
) -> str:
    """Direct knowledge-base retrieval through the shared registry source."""
    reg = registry if registry is not None else tool_registry.REGISTRY
    retrieve = reg.retrieval
    if retrieve is None:
        return 'Knowledge-base retrieval is not available on this deployment.'
    try:
        raw = await tool_registry.maybe_await(retrieve(user=user, query=query, k=k))
    except Exception:
        logger.exception('Voice retrieval failed for query %r.', query[:80])
        return 'The knowledge-base search failed; try again in a moment.'
    hits = [tool_registry.normalize_source(item) for item in (raw or [])]
    return format_retrieval_for_voice(hits)


def format_block_for_voice(block: dict[str, Any]) -> str:
    """Collapse a registry UI block into speakable text."""
    block_type = block.get('type')
    if block_type == 'error':
        title = block.get('title', 'The tool failed')
        return f'{title}: {block.get("error", "unknown error")}.'
    summary = str(block.get('summary') or block.get('title') or '').strip()
    if block_type == 'table':
        rows = block.get('rows') or []
        title = str(block.get('title') or '').strip()
        summary_text = str(block.get('summary') or '').strip()
        headline = ': '.join(part for part in (title, summary_text) if part)
        lines = [f'{headline} ({len(rows)} result(s)).'] if headline else []
        lines += [', '.join(str(cell) for cell in row) for row in rows[:5]]
        return '\n'.join(lines) if lines else 'The tool returned an empty result.'
    if block_type == 'wafer_map':
        return summary or 'Wafer map analysis completed.'
    return str(block.get('content') or summary or 'The tool returned no content.')


async def registry_tool_for_voice(
    *,
    user: Any,
    name: str,
    registry: tool_registry.ToolRegistry | None = None,
    **kwargs: Any,
) -> str:
    """Run one shared-registry tool and speak its block (availability-aware).

    Unknown or unavailable tools answer an error block from
    ``ToolRegistry.execute`` -- a missing integration degrades the tool,
    never the session.
    """
    reg = registry if registry is not None else tool_registry.REGISTRY
    block = await reg.execute(name, user, **kwargs)
    return format_block_for_voice(block)


class AthenaVoiceAgent(Agent):
    """Voice surface for the shared agent core (livekit-agents 1.x pattern).

    ``user``/``conversation`` may be None in degraded rooms (an unknown or
    generic room); tools then answer honest degraded messages instead of
    failing the turn.
    """

    def __init__(
        self,
        *,
        user: Any = None,
        conversation: Any = None,
        instructions: str | None = None,
        llm: Any = None,
        registry: tool_registry.ToolRegistry | None = None,
        retrieve: Any = None,
    ) -> None:
        self.user = user
        self.conversation = conversation
        self._llm = llm
        self._registry = registry
        self._retrieve = retrieve
        # One request id per agent instance: every shared-core turn it runs
        # traces to this voice session in the logs.
        self._request_id = f'voice-{uuid4().hex[:12]}'
        super().__init__(instructions=instructions or SYSTEM_PROMPT)

    @function_tool
    async def consult_athena(self, context: RunContext, query: str) -> str:
        """Answer a semiconductor fab or process engineering question with the full Athena agent.

        Runs one complete agent turn through the shared core (knowledge-base
        context, fab tools, web search, multi-round reasoning) and returns
        the final answer.

        Args:
            query: The question to answer, e.g. ``why did bin 3 yield drop this week?``
        """
        del context  # the shared core owns conversation state; no session access needed
        return await run_voice_turn(
            user=self.user,
            conversation=self.conversation,
            user_input=query,
            request_id=self._request_id,
            llm=self._llm,
            registry=self._registry,
            retrieve=self._retrieve,
        )

    @function_tool
    async def search_fab_knowledge(self, context: RunContext, query: str) -> str:
        """Search the fab knowledge base and return cited snippets.

        Args:
            query: What to look up, e.g. ``oxide etch rate spec``.
        """
        del context
        return await retrieve_for_voice(user=self.user, query=query, registry=self._registry)

    @function_tool
    async def web_search(self, context: RunContext, query: str) -> str:
        """Search the public web for current information via the shared registry tool.

        Args:
            query: The web search to run.
        """
        del context
        return await registry_tool_for_voice(
            user=self.user, name='web_search', query=query, registry=self._registry
        )
