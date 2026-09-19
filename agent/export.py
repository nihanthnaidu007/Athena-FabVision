"""Markdown export for conversations (spec #9; reused by the RCA report generator).

Pure functions: no ORM access, no I/O. ``conversation_to_markdown`` takes
a conversation and its already-fetched messages and returns the document
text, so the chat view serves it as a download and feature 11 assembles
RCA/8D reports through the same plumbing. Unknown tool-block types render
as fenced JSON -- an export never crashes on a block it predates.
"""

from __future__ import annotations

import json
from typing import Any

# Blocks with more points than this export as a note instead of a table,
# keeping the document bounded for long sensor series.
MAX_SERIES_POINTS_EXPORTED = 50

_ROLE_LABELS = {
    'user': 'User',
    'assistant': 'Assistant',
    'system': 'System note',
    'tool': 'Tool result',
}


def _cell(value: Any) -> str:
    """One table cell: pipes and newlines would break the Markdown table."""
    text = '' if value is None else str(value)
    return text.replace('|', '\\|').replace('\n', ' ')


def _markdown_table(columns: list[str], rows: list[list[Any]]) -> str:
    """Render a Markdown table; empty input yields an empty string."""
    if not columns:
        return ''
    lines = [
        '| ' + ' | '.join(_cell(column) for column in columns) + ' |',
        '| ' + ' | '.join('---' for _ in columns) + ' |',
    ]
    for row in rows:
        lines.append('| ' + ' | '.join(_cell(cell) for cell in row) + ' |')
    return '\n'.join(lines)


def _stat_rows(block: dict[str, Any], keys: list[tuple[str, str]]) -> list[list[str]]:
    """Two-column metric rows for the keys a block actually carries."""
    rows = []
    for key, label in keys:
        if block.get(key) is not None:
            rows.append([label, _cell(block[key])])
    return rows


def _list_items(items: list[str]) -> str:
    return '\n'.join(f'- {item}' for item in items)


def _table_block_markdown(block: dict[str, Any]) -> str:
    body = _markdown_table(
        [str(column) for column in block.get('columns', [])],
        [list(row) for row in block.get('rows', [])],
    )
    return '\n\n'.join(part for part in (block.get('summary') or '', body) if part)


def _wafer_block_markdown(block: dict[str, Any]) -> str:
    rows = _stat_rows(
        block,
        [
            ('total_dies', 'Total dies'),
            ('pass_count', 'Pass'),
            ('fail_count', 'Fail'),
            ('yield_pct', 'Yield'),
            ('edge_ring_score', 'Edge-ring score'),
            ('center_hotspot_score', 'Center hotspot score'),
        ],
    )
    parts = [block.get('summary') or '']
    table = _markdown_table(['Metric', 'Value'], rows)
    if table:
        parts.append(table)
    patterns = block.get('patterns') or []
    if patterns:
        parts.append('Patterns: ' + ', '.join(str(pattern) for pattern in patterns))
    else:
        parts.append('No spatial patterns above threshold.')
    bins = block.get('bin_counts') or {}
    if bins:
        parts.append(
            _markdown_table(
                [f'bin {code}' for code in bins],
                # One compact row: bin names as columns, counts beneath.
                [[bins[code] for code in bins]],
            )
        )
    issues = block.get('issues') or []
    if issues:
        parts.append(_list_items([str(issue) for issue in issues]))
    # The die lattice is the SVG's data, not report prose; the count says so.
    dies = block.get('dies') or []
    if dies or block.get('dies_omitted'):
        omitted_clause = ''
        if block.get('dies_omitted'):
            omitted_clause = f", {block['dies_omitted']} omitted by downsampling"
        parts.append(
            f'Die-level lattice: {len(dies)} dies in the on-screen grid{omitted_clause}.'
        )
    return '\n\n'.join(part for part in parts if part)


def _spc_block_markdown(block: dict[str, Any]) -> str:
    rows = _stat_rows(
        block,
        [
            ('mean', 'Mean'),
            ('sigma', 'Sigma'),
            ('ucl', 'UCL'),
            ('lcl', 'LCL'),
            ('verdict', 'Verdict'),
        ],
    )
    parts = [block.get('summary') or '']
    table = _markdown_table(['Metric', 'Value'], rows)
    if table:
        parts.append(table)
    values = block.get('values') or []
    flags = block.get('point_flags') or []
    if values and len(values) <= MAX_SERIES_POINTS_EXPORTED:
        parts.append(
            _markdown_table(
                ['Point', 'Value', 'Flags'],
                [
                    [index + 1, value, ', '.join(flags[index]) if index < len(flags) else '']
                    for index, value in enumerate(values)
                ],
            )
        )
    elif values:
        parts.append(f'Series of {len(values)} points omitted from export.')
    violations = block.get('violations') or []
    if violations:
        parts.append(
            _list_items(
                f'{violation.get("label") or violation.get("rule") or "Rule violation"}'
                + (f' — {violation["detail"]}' if violation.get('detail') else '')
                for violation in violations
            )
        )
    else:
        parts.append('No Nelson-rule violations detected.')
    references = block.get('references') or []
    if references:
        parts.append(_list_items(f'Reference: {reference}' for reference in references))
    issues = block.get('issues') or []
    if issues:
        parts.append(_list_items(str(issue) for issue in issues))
    return '\n\n'.join(part for part in parts if part)


def render_tool_block_markdown(block: dict[str, Any]) -> str:
    """One tool block as human-readable Markdown.

    The structured block types render as prose, stats, and tables;
    anything unknown (a block added after this code was written) renders
    as fenced JSON so the export stays complete and lossless.
    """
    if not isinstance(block, dict):
        return str(block)
    block_type = block.get('type')
    if block_type == 'table':
        body = _table_block_markdown(block)
    elif block_type == 'wafer_map':
        body = _wafer_block_markdown(block)
    elif block_type == 'spc_chart':
        body = _spc_block_markdown(block)
    elif block_type == 'error':
        detail = block.get('detail') or block.get('error') or ''
        return f'> Tool error: {detail}'
    elif block_type == 'text':
        body = block.get('summary') or block.get('text') or ''
    else:
        body = f'```json\n{json.dumps(block, indent=2, default=str, sort_keys=True)}\n```'
    title = block.get('title') or block.get('tool') or 'Tool result'
    return f'**{title}**\n\n{body}' if body else f'**{title}**'


def render_sources_markdown(sources: list[dict[str, Any]]) -> str:
    """The citation chips as a Markdown list (docs and tool results)."""
    lines: list[str] = []
    for source in sources or []:
        if not isinstance(source, dict):
            continue
        if source.get('kind') == 'doc':
            bits = [str(source.get('title') or 'Untitled document')]
            snippet = str(source.get('snippet') or '')
            if snippet:
                bits.append(f'"{snippet}"')
            if source.get('score') is not None:
                bits.append(f'relevance {source["score"]}')
            lines.append('- ' + ' — '.join(bits))
        elif source.get('kind') == 'tool':
            bits = [f'tool: {source.get("tool") or "unknown"}']
            if source.get('summary'):
                bits.append(str(source['summary']))
            lines.append('- ' + ' — '.join(bits))
    return '\n'.join(lines)


def render_message_markdown(message: Any) -> str:
    """One persisted message as a ``## <role>`` Markdown section."""
    role = _ROLE_LABELS.get(getattr(message, 'role', ''), 'Message')
    sections = [f'## {role}', '', str(message.content or '')]
    sources = render_sources_markdown(getattr(message, 'sources', None) or [])
    if sources:
        sections += ['', '### Sources', '', sources]
    for block in getattr(message, 'tool_blocks', None) or []:
        sections += ['', '### Tool result', '', render_tool_block_markdown(block)]
    return '\n'.join(sections)


def conversation_to_markdown(conversation: Any, messages: list[Any]) -> str:
    """The whole conversation as one Markdown document.

    Deterministic: content derives only from the conversation and its
    messages, never from the wall clock, so exports are snapshot-testable.
    """
    title = getattr(conversation, 'title', '') or 'Conversation'
    sections = [f'# {title}']
    if not messages:
        sections += ['_No messages yet._']
    sections += [render_message_markdown(message) for message in messages]
    return '\n\n'.join(sections).rstrip() + '\n'
