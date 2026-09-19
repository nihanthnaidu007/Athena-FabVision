"""One budgeted LLM call that assembles an 8D report from a conversation.

Spec #11: the ``excursion_triage`` tool stays the deterministic entry
point; the report is the artifact the investigation conversation
produced. The prompt and the reply schema are pure
(:mod:`fabtools.rca_report`); this module only consumes the streaming
LLMClient protocol -- tests script the reply and never touch the
network. The spend contract is exactly one LLM call per user action.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from fabtools import rca_report

from .llm import LLMClient

#: Error codes the chat view surfaces verbatim to the client.
CODE_GENERATION_FAILED = 'report_generation_failed'
CODE_REPORT_INVALID = 'report_invalid'


@dataclass(frozen=True)
class ReportAssembly:
    """The outcome of the one report-assembly call.

    ``report`` is None exactly when ``error`` is non-empty; ``code``
    says which stage failed (stream or schema); ``usage`` carries the
    token counts when the model reported them.
    """

    report: dict[str, Any] | None
    error: str = ''
    code: str = CODE_REPORT_INVALID
    usage: dict[str, int] = field(default_factory=dict)


async def assemble_report(*, title: str, transcript: str, llm: LLMClient) -> ReportAssembly:
    """Run the one budgeted LLM call and validate its reply into a report.

    Never raises: a failing stream or a schema-invalid reply degrades to
    ``ReportAssembly(report=None, error=...)`` so the caller surfaces an
    honest failure instead of a faked or partial report.
    """
    messages = rca_report.build_report_messages(title=title, transcript=transcript)
    parts: list[str] = []
    usage: dict[str, int] = {}
    try:
        async for event in llm.stream(messages=messages):
            if event.type == 'delta':
                parts.append(event.text)
            elif event.type == 'usage':
                usage = dict(event.usage)
    except Exception as exc:
        return ReportAssembly(None, f'{type(exc).__name__}: {exc}', CODE_GENERATION_FAILED, usage)
    try:
        payload = rca_report.extract_json_payload(''.join(parts))
        report = rca_report.validate_report(payload)
    except rca_report.ReportValidationError as exc:
        return ReportAssembly(None, str(exc), CODE_REPORT_INVALID, usage)
    return ReportAssembly(report, '', CODE_REPORT_INVALID, usage)
