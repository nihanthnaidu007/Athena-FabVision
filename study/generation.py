"""One budgeted LLM call per generate action (spec #6).

``generate_flashcards_for_notebook`` grounds a single completion on the
notebook's ready documents, validates the response against the
flashcard schema, and persists nothing unless every card passes. The
call is metered as a UsageEvent (kind=study) whether or not the
payload survives validation -- the spend happened either way. All
failures are typed so the view can answer each one honestly; garbage
never persists.

Zero-network tests inject a scripted ``LLMClient`` (agent.tests.fakes);
the review path (``study.scheduler``) never enters this module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from django.db import transaction
from django.utils import timezone

from agent.llm import LLMClient, default_client
from assistant.models import Chunk, Document, UsageEvent
from assistant.usage import record_usage

from .models import Flashcard

logger = logging.getLogger(__name__)

#: Schema bounds: a generation run yields 1..20 cards with bounded
#: question/answer lengths -- a payload outside this is rejected whole,
#: never partially persisted.
MAX_FLASHCARDS_PER_RUN = 20
QUESTION_MAX_CHARS = 500
ANSWER_MAX_CHARS = 2000
#: The grounding budget for the one call, in characters of chunk text.
CONTEXT_CHAR_LIMIT = 12_000

GENERATION_SYSTEM_PROMPT = (
    'You create study flashcards for semiconductor fab and process engineering '
    'from course material. Output ONLY valid JSON matching exactly this schema: '
    '{"flashcards": [{"question": str, "answer": str}, ...]} '
    'with 1 to 20 cards. Questions are self-contained and specific; answers are '
    'concise and correct. No markdown fences, no commentary, no extra keys '
    'beyond "flashcards".'
)


class GenerationError(Exception):
    """Base: flashcard generation failed; the message is user-safe."""


class GenerationUnavailable(GenerationError):
    """No LLM is configured on this deployment."""


class GenerationEmpty(GenerationError):
    """The notebook has nothing to ground on (no ready documents/chunks)."""


class GenerationInvalidResponse(GenerationError):
    """The model's response failed the flashcard schema."""


def collect_notebook_context(notebook, *, max_chars: int = CONTEXT_CHAR_LIMIT) -> str:
    """The notebook's ready chunk text, capped at ``max_chars``.

    Deterministic order (document id, then chunk index) and a hard
    budget keep the one budgeted call bounded however large the
    notebook grows. Empty chunks are skipped; the cap stops at a chunk
    boundary rather than splitting one.
    """
    chunks = (
        Chunk.objects.filter(
            document__notebook=notebook, document__status=Document.Status.READY
        )
        .select_related('document')
        .order_by('document_id', 'index')
    )
    parts: list[str] = []
    used = 0
    for chunk in chunks:
        if not chunk.content:
            continue
        part = f'[{chunk.document.original_filename}] {chunk.content}'
        if used + len(part) > max_chars:
            break
        parts.append(part)
        used += len(part)
    return '\n\n'.join(parts)


def _strip_code_fence(raw: str) -> str:
    """Tolerate one ```json-fenced block around the payload."""
    text = raw.strip()
    if text.startswith('```'):
        first_newline = text.find('\n')
        if first_newline != -1:
            text = text[first_newline + 1 :]
        closing = text.rstrip().rfind('```')
        if closing != -1:
            text = text.rstrip()[:closing]
    return text.strip()


def parse_flashcards(raw: str) -> list[dict[str, str]]:
    """Validate the model's output against the flashcard schema.

    Returns a list of ``{'question': ..., 'answer': ...}`` with
    stripped, bounded strings, or raises ``GenerationInvalidResponse``
    naming the first violation. Any violation rejects the whole
    payload: partial persistence would quietly bless garbage.
    """
    text = _strip_code_fence(raw)
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise GenerationInvalidResponse('the model did not return valid JSON.') from exc
    if not isinstance(payload, dict) or not isinstance(payload.get('flashcards'), list):
        raise GenerationInvalidResponse('expected a {"flashcards": [...]} object.')
    items = payload['flashcards']
    if not items:
        raise GenerationInvalidResponse('the model returned no flashcards.')
    if len(items) > MAX_FLASHCARDS_PER_RUN:
        raise GenerationInvalidResponse(
            f'the model returned {len(items)} cards; the limit is {MAX_FLASHCARDS_PER_RUN}.'
        )
    cards: list[dict[str, str]] = []
    for position, item in enumerate(items, start=1):
        if not isinstance(item, dict) or 'question' not in item or 'answer' not in item:
            raise GenerationInvalidResponse(f'card {position} is missing question/answer.')
        question = item['question'] if isinstance(item['question'], str) else ''
        answer = item['answer'] if isinstance(item['answer'], str) else ''
        question, answer = question.strip(), answer.strip()
        if not question or not answer:
            raise GenerationInvalidResponse(f'card {position} has an empty question or answer.')
        if len(question) > QUESTION_MAX_CHARS:
            raise GenerationInvalidResponse(
                f'card {position} question exceeds {QUESTION_MAX_CHARS} characters.'
            )
        if len(answer) > ANSWER_MAX_CHARS:
            raise GenerationInvalidResponse(
                f'card {position} answer exceeds {ANSWER_MAX_CHARS} characters.'
            )
        cards.append({'question': question, 'answer': answer})
    return cards


async def _consume_llm(llm: LLMClient, messages: list[dict]) -> tuple[str, dict[str, int]]:
    """One completion round: accumulated text plus token usage."""
    parts: list[str] = []
    usage = {'tokens_in': 0, 'tokens_out': 0}
    try:
        async for event in llm.stream(messages=messages, tools=None):
            if event.type == 'delta':
                parts.append(event.text)
            elif event.type == 'usage':
                usage['tokens_in'] += event.usage.get('tokens_in', 0)
                usage['tokens_out'] += event.usage.get('tokens_out', 0)
    except Exception as exc:
        raise GenerationError(f'the model call failed: {exc}') from exc
    return ''.join(parts), usage


def _record_generation_usage(user, llm: LLMClient, usage: dict[str, int], *, latency_ms: int):
    """Meter the call even when validation fails: the spend happened."""
    return record_usage(
        user=user,
        kind=UsageEvent.Kind.STUDY,
        model_name=getattr(llm, 'model_name', ''),
        tokens_in=usage['tokens_in'],
        tokens_out=usage['tokens_out'],
        latency_ms=latency_ms,
    )


def generate_flashcards_for_notebook(
    *, user, notebook, llm: LLMClient | None = None, now=None
) -> list[Flashcard]:
    """Generate and persist flashcards for ``notebook`` in one LLM call.

    Raises ``GenerationUnavailable`` with no configured LLM, and
    ``GenerationEmpty`` when the notebook has no ready chunk text --
    both before any call is spent. Everything else (call failure,
    invalid JSON, schema garbage) raises ``GenerationError`` after the
    metered call; nothing persists unless the whole payload validated.
    """
    client = llm if llm is not None else default_client()
    if client is None:
        raise GenerationUnavailable('No LLM is configured on this deployment.')
    context_text = collect_notebook_context(notebook)
    if not context_text:
        raise GenerationEmpty('This notebook has no ready documents to ground on.')

    messages = [
        {'role': 'system', 'content': GENERATION_SYSTEM_PROMPT},
        {'role': 'user', 'content': f'Course material:\n\n{context_text}'},
    ]
    t0 = time.monotonic()
    raw_text, usage = asyncio.run(_consume_llm(client, messages))
    _record_generation_usage(user, client, usage, latency_ms=int((time.monotonic() - t0) * 1000))
    cards_payload = parse_flashcards(raw_text)

    due_at = now if now is not None else timezone.now()
    with transaction.atomic():
        flashcards = [
            Flashcard.objects.create(
                user=user,
                notebook=notebook,
                question=card['question'],
                answer=card['answer'],
                due_at=due_at,
            )
            for card in cards_payload
        ]
    logger.info(
        'Generated %d flashcards from notebook %d (user=%s).',
        len(flashcards),
        notebook.pk,
        getattr(user, 'pk', None),
    )
    return flashcards
