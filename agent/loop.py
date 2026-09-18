"""The shared async agent loop.

One turn of conversation: persist the user message, retrieve KB
context, build tool schemas and prompt messages from history, then
stream the LLM answer -- executing tool calls and feeding their result
blocks back into the model round by round.

``run_agent`` is an async generator yielding typed agent events:
``status``, ``delta``, ``tool_call``, ``tool_result``, ``sources``,
``done``, ``error``. Any failure becomes an ``error`` event; the
stream degrades, it never dies. Assistant text produced before a
failure is still persisted, so an aborted or failing turn keeps its
partial answer.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from asgiref.sync import sync_to_async

from assistant.models import Conversation, Message
from assistant.usage import record_usage

from . import registry as tool_registry
from .llm import LLMClient, ToolCallRequest, default_client, tool_result_content

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    'You are Athena, an AI assistant for semiconductor fab and process engineering. '
    'Ground answers in retrieved knowledge-base context and cite it; use tools for wafer '
    'data analysis and current information; be concise, quantitative, and honest about '
    'uncertainty.'
)

TUTOR_SYSTEM_PROMPT = (
    'You are Athena, a Socratic tutor for semiconductor fab and process engineering. '
    "Never give the final answer or a complete solution: reply with hints, guiding "
    'questions, and one next step at a time, and check the student\'s understanding as '
    'you go. Ground hints in retrieved knowledge-base context and cite it; you may run '
    'tools, but guide the student through interpreting each result instead of stating '
    'the conclusion.'
)


def resolve_system_prompt(mode: str) -> str:
    """Pick the system preset for a conversation mode.

    Tutor conversations swap the default note for the tutor preset;
    anything unknown answers as the default assistant -- a corrupt
    value can never silently turn tutoring on.
    """
    if mode == Conversation.Mode.TUTOR:
        return TUTOR_SYSTEM_PROMPT
    return SYSTEM_PROMPT


HISTORY_MESSAGE_LIMIT = 30
TOOL_ROUNDS_LIMIT = 5
RETRIEVAL_K = 5

EVENT_STATUS = 'status'
EVENT_DELTA = 'delta'
EVENT_TOOL_CALL = 'tool_call'
EVENT_TOOL_RESULT = 'tool_result'
EVENT_SOURCES = 'sources'
EVENT_DONE = 'done'
EVENT_ERROR = 'error'


@dataclass(frozen=True)
class AgentEvent:
    """Everything the stream can emit; the SSE view serializes these."""

    type: str
    data: dict = field(default_factory=dict)


def build_messages(
    *,
    system_prompt: str,
    history: list[Message],
    user_input: str,
    context: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compose the chat messages list sent to the model.

    System prompt, then the last HISTORY_MESSAGE_LIMIT user/assistant
    turns, an optional KB-context system note, and the new user turn.
    """
    messages: list[dict[str, Any]] = [
        {'role': 'system', 'content': system_prompt},
        *[
            {'role': message.role, 'content': message.content}
            for message in history
            if message.role in (Message.Role.USER, Message.Role.ASSISTANT)
        ],
    ]
    if context:
        context_text = '\n\n'.join(
            f"[{source['title']}] {source['snippet']}" for source in context
        )
        messages.append(
            {
                'role': 'system',
                'content': 'Knowledge-base context; cite it when used:\n' + context_text,
            }
        )
    messages.append({'role': 'user', 'content': user_input})
    return messages


def tool_summary_source(name: str, block: dict[str, Any]) -> dict[str, Any]:
    """Collapse a tool result block into one Message.sources entry."""
    return {
        'kind': 'tool',
        'tool': name,
        'summary': str(block.get('summary') or block.get('title') or ''),
    }


async def _create_message(conversation: Conversation, **fields: Any) -> Message:
    return await sync_to_async(Message.objects.create)(conversation=conversation, **fields)


async def _load_history(conversation: Conversation, limit: int, exclude_pk: int) -> list[Message]:
    """The last ``limit`` messages before the current turn, oldest first.

    The just-persisted user message is excluded; ``build_messages``
    appends it explicitly after the KB context.
    """

    def _query() -> list[Message]:
        recent = (
            conversation.messages.exclude(pk=exclude_pk).order_by('-created_at', '-pk')[:limit]
        )
        return list(reversed(recent))

    return await sync_to_async(_query)()


async def _fetch_context(retrieve: Any, user: Any, query: str) -> list[dict[str, Any]]:
    """Run the wired retrieval source and normalize its hits (never raises)."""
    if retrieve is None:
        return []
    try:
        raw = await tool_registry.maybe_await(retrieve(user=user, query=query, k=RETRIEVAL_K))
    except Exception:
        logger.exception('Retrieval source failed; answering without KB context.')
        return []
    if not raw:
        return []
    return [tool_registry.normalize_source(item) for item in raw]


async def run_agent(
    *,
    user: Any,
    conversation: Conversation,
    user_input: str,
    llm: LLMClient | None = None,
    registry: tool_registry.ToolRegistry | None = None,
    retrieve: Any = None,
    request_id: str = '',
    api_key: Any = None,
    history_limit: int = HISTORY_MESSAGE_LIMIT,
    max_tool_rounds: int = TOOL_ROUNDS_LIMIT,
) -> AsyncIterator[AgentEvent]:
    """Stream one agent turn as typed events; persist it and its usage.

    Errors never propagate: every failure inside the turn becomes an
    ``error`` event, and whatever assistant text was already produced is
    persisted so an aborted turn keeps its partial answer.
    """
    reg = registry if registry is not None else tool_registry.REGISTRY
    retrieve = retrieve if retrieve is not None else reg.retrieval
    t0 = time.monotonic()
    text_parts: list[str] = []
    tool_blocks: list[dict[str, Any]] = []
    tool_sources: list[dict[str, Any]] = []
    kb_context: list[dict[str, Any]] = []
    usage = {'tokens_in': 0, 'tokens_out': 0}
    turn_persisted = False

    def _rid(data: dict) -> dict:
        return {**data, 'request_id': request_id}

    def _final_sources() -> list[dict[str, Any]]:
        return [{'kind': 'doc', **source} for source in kb_context] + tool_sources

    async def _persist_turn() -> None:
        """Persist the assistant turn (full or partial) and its usage once.

        Only turns that produced content are persisted: a stream that
        failed before any output leaves just the user message behind.
        """
        nonlocal turn_persisted
        if turn_persisted or not (text_parts or tool_blocks):
            return
        turn_persisted = True
        content = ''.join(text_parts)
        sources = _final_sources()
        try:
            assistant_message = await _create_message(
                conversation,
                role=Message.Role.ASSISTANT,
                content=content,
                sources=sources,
                tool_blocks=tool_blocks,
            )
        except Exception:
            logger.exception('Failed to persist assistant message (rid=%s).', request_id)
            return
        try:
            await sync_to_async(record_usage)(
                user=user,
                kind='chat',
                api_key=api_key,
                conversation=conversation,
                message=assistant_message,
                tokens_in=usage['tokens_in'],
                tokens_out=usage['tokens_out'],
                latency_ms=int((time.monotonic() - t0) * 1000),
                model_name=getattr(llm, 'model_name', '') if llm else '',
            )
        except Exception:
            logger.exception('Failed to record agent usage (rid=%s).', request_id)

    try:
        logger.info(
            'Agent turn started (rid=%s user=%s conversation=%s).',
            request_id,
            getattr(user, 'pk', None),
            conversation.pk,
        )
        user_message = await _create_message(
            conversation, role=Message.Role.USER, content=user_input
        )
        yield AgentEvent(
            EVENT_STATUS,
            _rid({
                'stage': 'started',
                'conversation_id': conversation.pk,
                'message_id': user_message.pk,
            }),
        )

        llm = llm if llm is not None else default_client()
        if llm is None:
            logger.warning('Agent turn degraded: no LLM configured (rid=%s).', request_id)
            yield AgentEvent(
                EVENT_ERROR,
                _rid({
                    'code': 'llm_unavailable',
                    'error': 'The model is not configured on this deployment.',
                }),
            )
            return

        yield AgentEvent(EVENT_STATUS, _rid({'stage': 'retrieving'}))
        kb_context = await _fetch_context(retrieve, user, user_input)

        history = await _load_history(conversation, history_limit, exclude_pk=user_message.pk)
        messages = build_messages(
            system_prompt=resolve_system_prompt(conversation.mode),
            history=history,
            user_input=user_input,
            context=kb_context,
        )
        tools = reg.tool_schemas()

        for _round in range(max_tool_rounds):
            yield AgentEvent(EVENT_STATUS, _rid({'stage': 'generating'}))
            round_calls: list[ToolCallRequest] = []
            async for event in llm.stream(messages=messages, tools=tools or None):
                if event.type == 'delta':
                    text_parts.append(event.text)
                    yield AgentEvent(EVENT_DELTA, _rid({'text': event.text}))
                elif event.type == 'tool_call' and event.tool_call is not None:
                    round_calls.append(event.tool_call)
                elif event.type == 'usage':
                    usage['tokens_in'] += event.usage.get('tokens_in', 0)
                    usage['tokens_out'] += event.usage.get('tokens_out', 0)

            if not round_calls:
                break

            messages.append(
                {
                    'role': 'assistant',
                    'content': ''.join(text_parts) or None,
                    'tool_calls': [
                        {
                            'id': call.id,
                            'type': 'function',
                            'function': {'name': call.name, 'arguments': call.arguments},
                        }
                        for call in round_calls
                    ],
                }
            )
            for call in round_calls:
                arguments = _safe_arguments(call)
                yield AgentEvent(
                    EVENT_TOOL_CALL,
                    _rid({'name': call.name, 'arguments': arguments, 'call_id': call.id}),
                )
                block = await reg.execute(call.name, user, **arguments)
                tool_blocks.append(block)
                tool_sources.append(tool_summary_source(call.name, block))
                logger.info(
                    'Tool %s executed (rid=%s block_type=%s).',
                    call.name,
                    request_id,
                    block.get('type'),
                )
                yield AgentEvent(EVENT_TOOL_RESULT, _rid({'name': call.name, 'block': block}))
                messages.append(
                    {
                        'role': 'tool',
                        'tool_call_id': call.id,
                        'content': tool_result_content(block),
                    }
                )

        sources = _final_sources()
        yield AgentEvent(EVENT_SOURCES, _rid({'sources': sources}))
        latency_ms = int((time.monotonic() - t0) * 1000)
        yield AgentEvent(
            EVENT_DONE,
            _rid({
                'conversation_id': conversation.pk,
                'latency_ms': latency_ms,
                'tokens_in': usage['tokens_in'],
                'tokens_out': usage['tokens_out'],
            }),
        )
        logger.info(
            'Agent turn done (rid=%s latency_ms=%s tokens_in=%s tokens_out=%s).',
            request_id,
            latency_ms,
            usage['tokens_in'],
            usage['tokens_out'],
        )
    except Exception as exc:
        logger.exception('Agent turn failed (rid=%s).', request_id)
        yield AgentEvent(
            EVENT_ERROR,
            _rid({'code': 'agent_error', 'error': f'The assistant turn failed: {exc}'}),
        )
    finally:
        await _persist_turn()


def _safe_arguments(call: ToolCallRequest) -> dict[str, Any]:
    """Parse a tool-call's JSON arguments; malformed arguments become {}."""
    try:
        parsed = json.loads(call.arguments or '{}')
    except json.JSONDecodeError:
        logger.warning('Tool call %s had unparseable arguments.', call.name)
        return {}
    return parsed if isinstance(parsed, dict) else {}
