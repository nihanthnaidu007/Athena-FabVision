"""Structured 8D report assembly from an excursion conversation (spec #11).

The companion of the excursion_triage tool: triage is the deterministic
entry point that flags the excursion, the investigation then happens in
chat, and this module turns the finished conversation into a structured
8D (Eight Disciplines) draft. The LLM call itself is budgeted to one per
user action (the locked v1.1 spend decision) and lives in
``agent.rca.assemble_report``; everything around that call is pure and
offline:

- :func:`build_report_messages` composes the prompt from the
  conversation's Markdown transcript -- the same export plumbing the
  chat export uses -- capped to ``MAX_TRANSCRIPT_CHARS`` with a visible
  truncation marker.
- :func:`extract_json_payload` recovers the JSON object from the model
  reply, tolerating code fences and stray prose.
- :func:`validate_report` enforces the report schema honestly: required
  sections present and non-empty, free-text owners, unknown extra keys
  ignored. A reply that fails validation raises
  :class:`ReportValidationError` naming the field, which the caller
  surfaces verbatim -- a report is never faked from garbage.

Nothing here touches the network, the database, or the wall clock.
"""

from __future__ import annotations

import json
import re
from typing import Any

#: The transcript bound. Long conversations keep their head (where the
#: excursion started) and tail (the latest findings); the dropped middle
#: is announced by a marker, never silently summarized.
MAX_TRANSCRIPT_CHARS = 24_000
_MIDDLE_MARKER = '\n\n[… middle of the transcript omitted …]\n\n'

#: The exact JSON shape the system prompt demands from the model.
REPORT_JSON_SHAPE = (
    '{'
    '"title": string, '
    '"team": [string], '
    '"problem": string, '
    '"timeline": [{"when": string, "event": string}], '
    '"containment": [string], '
    '"root_cause": string, '
    '"root_cause_candidates": [string], '
    '"corrective_actions": [{"action": string, "owner": string, "due": string}], '
    '"verification": [string], '
    '"prevention": [string], '
    '"closure": string'
    '}'
)

RCA_SYSTEM_PROMPT = (
    'You assemble an 8D (Eight Disciplines) problem-solving report from a fab '
    'excursion investigation conversation. Reply with ONE JSON object and nothing '
    'else -- no prose, no code fences. The JSON shape is: ' + REPORT_JSON_SHAPE + '. '
    "Extract the incident timeline from the dates and events the turns state; owners "
    'and due dates are free text exactly as the conversation states them. Use only '
    'facts from the conversation: when it does not state something, return an empty '
    'list or empty string for it -- never invent facts. Required and non-empty: '
    'title, problem, timeline, containment, root_cause, corrective_actions.'
)

# A fenced block is preferred; greedy to the last } inside the fence so
# nested objects (timeline entries) survive the extraction.
_FENCE_RE = re.compile(r'```(?:json)?\s*(\{.*\})\s*```', re.DOTALL)


class ReportValidationError(ValueError):
    """The model reply does not match the 8D schema; the message names why."""


def bounded_transcript(transcript: str, *, cap: int = MAX_TRANSCRIPT_CHARS) -> str:
    """The transcript capped to ``cap`` characters, keeping head and tail."""
    if len(transcript) <= cap:
        return transcript
    half = max((cap - len(_MIDDLE_MARKER)) // 2, 1)
    return transcript[:half] + _MIDDLE_MARKER + transcript[-half:]


def build_report_messages(title: str, transcript: str) -> list[dict[str, str]]:
    """The prompt messages for the one budgeted report-assembly call."""
    return [
        {'role': 'system', 'content': RCA_SYSTEM_PROMPT},
        {
            'role': 'user',
            'content': (
                f'Conversation title: {title}\n\n'
                f'Transcript:\n\n{bounded_transcript(transcript)}\n\n'
                'Return the 8D report JSON object now.'
            ),
        },
    ]


def extract_json_payload(reply: str) -> Any:
    """Recover the JSON value from the model reply.

    Fenced JSON wins (the system prompt forbids fences, models add them
    anyway); otherwise the span from the first ``{`` to the last ``}``
    is parsed. Anything else raises :class:`ReportValidationError`.
    """
    text = (reply or '').strip()
    if not text:
        raise ReportValidationError('the model returned an empty reply')
    fenced = _FENCE_RE.search(text)
    if fenced is None and not text.startswith('{'):
        start, end = text.find('{'), text.rfind('}')
        if start == -1 or end <= start:
            raise ReportValidationError('the model reply contains no JSON object')
        text = text[start:end + 1]
    try:
        return json.loads(fenced.group(1) if fenced else text)
    except ValueError as exc:
        raise ReportValidationError(f'the model reply is not valid JSON ({exc})') from None


def _clean_text(value: Any, field: str, *, required: bool = False) -> str:
    """One schema string: stripped; scalars coerced; missing required is an error."""
    if value is None:
        if required:
            raise ReportValidationError(f"field '{field}' is missing")
        return ''
    if isinstance(value, (dict, list)):
        raise ReportValidationError(f"field '{field}' must be text, got {type(value).__name__}")
    text = str(value).strip()
    if required and not text:
        raise ReportValidationError(f"field '{field}' must not be empty")
    return text


def _clean_string_list(value: Any, field: str, *, required: bool = False) -> list[str]:
    """One schema string list; empty entries are named, not dropped."""
    if value is None:
        if required:
            raise ReportValidationError(f"field '{field}' is missing")
        return []
    if not isinstance(value, list):
        raise ReportValidationError(f"field '{field}' must be a list, got {type(value).__name__}")
    items = []
    for index, item in enumerate(value):
        if isinstance(item, (dict, list)):
            raise ReportValidationError(
                f"field '{field}' entry {index + 1} must be text, got {type(item).__name__}"
            )
        text = str(item).strip() if item is not None else ''
        if not text:
            raise ReportValidationError(f"field '{field}' entry {index + 1} must not be empty")
        items.append(text)
    if required and not items:
        raise ReportValidationError(f"field '{field}' must contain at least one entry")
    return items


def _clean_timeline(value: Any) -> list[dict[str, str]]:
    """Timeline entries: ``{when, event}`` with both fields non-empty."""
    if value is None:
        raise ReportValidationError("field 'timeline' is missing")
    if not isinstance(value, list) or not value:
        raise ReportValidationError("field 'timeline' must be a non-empty list")
    entries = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ReportValidationError(
                f"field 'timeline' entry {index + 1} must be an object with 'when' and 'event'"
            )
        when_field = f'timeline entry {index + 1} "when"'
        event_field = f'timeline entry {index + 1} "event"'
        entries.append({
            'when': _clean_text(item.get('when'), when_field, required=True),
            'event': _clean_text(item.get('event'), event_field, required=True),
        })
    return entries


def _clean_actions(value: Any) -> list[dict[str, str]]:
    """Corrective actions: ``action`` required; ``owner``/``due`` free text."""
    if value is None:
        raise ReportValidationError("field 'corrective_actions' is missing")
    if not isinstance(value, list) or not value:
        raise ReportValidationError("field 'corrective_actions' must be a non-empty list")
    actions = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise ReportValidationError(
                f"field 'corrective_actions' entry {index + 1} must be an object "
                "with 'action', 'owner', and 'due'"
            )
        action_field = f'corrective_actions entry {index + 1} "action"'
        owner_field = f'corrective_actions entry {index + 1} "owner"'
        due_field = f'corrective_actions entry {index + 1} "due"'
        actions.append({
            'action': _clean_text(item.get('action'), action_field, required=True),
            'owner': _clean_text(item.get('owner'), owner_field),
            'due': _clean_text(item.get('due'), due_field),
        })
    return actions


def validate_report(payload: Any) -> dict[str, Any]:
    """Validate and normalize the model's payload into the report dict.

    Required sections (``title``, ``problem``, ``timeline``,
    ``containment``, ``root_cause``, ``corrective_actions``) must be
    present and non-empty; optional ones default to empty. Unknown extra
    keys are ignored, so a model that volunteers an extra section cannot
    break the assembly.
    """
    if not isinstance(payload, dict):
        raise ReportValidationError(
            f'the report must be a JSON object, got {type(payload).__name__}'
        )
    return {
        'title': _clean_text(payload.get('title'), 'title', required=True),
        'team': _clean_string_list(payload.get('team'), 'team'),
        'problem': _clean_text(payload.get('problem'), 'problem', required=True),
        'timeline': _clean_timeline(payload.get('timeline')),
        'containment': _clean_string_list(payload.get('containment'), 'containment', required=True),
        'root_cause': _clean_text(payload.get('root_cause'), 'root_cause', required=True),
        'root_cause_candidates': _clean_string_list(
            payload.get('root_cause_candidates'), 'root_cause_candidates'
        ),
        'corrective_actions': _clean_actions(payload.get('corrective_actions')),
        'verification': _clean_string_list(payload.get('verification'), 'verification'),
        'prevention': _clean_string_list(payload.get('prevention'), 'prevention'),
        'closure': _clean_text(payload.get('closure'), 'closure'),
    }
