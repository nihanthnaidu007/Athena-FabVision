"""Tool registry per the integration contract.

``TOOLS`` is a list of ``[name, fn, availability_fn]`` entries. Every ``fn``
is an async callable ``(user, **kwargs)`` returning a block dict
``{type, title, ...}`` where ``type`` is one of ``table | text | wafer_map |
spc_chart | error``. ``availability_fn`` is a zero-argument predicate consumed before
the tool is offered to the model — it must never raise. Consumers import
this module defensively (try/except ImportError) and feature-gate on the
availability functions, so the five parallel PRs may merge in any order.

- ``wafer_map_analyze`` / ``excursion_triage`` are deterministic and always
  available.
- ``kb_search`` delegates to ``rag.retrieval.retrieve`` (the contract entry
  point) and is unavailable when the RAG package is missing.
- ``web_search`` uses an injectable :class:`~fabtools.search.WebSearchClient`
  and degrades visibly (structured error block) when no client is
  configured — it never crashes and never requires a key at import time.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from fabtools import excursion, spc, wafer_map
from fabtools.search import WebSearchClient, default_web_search_client

ToolFn = Callable[..., Awaitable[dict[str, Any]]]
AvailabilityFn = Callable[[], bool]

Block = dict[str, Any]

# Lazy, cached lookup of the RAG retrieval entry point (the contract's
# rag.retrieval.retrieve). Module-level so tests can inject fakes by setting
# fabtools.tools._rag_retrieve and marking _rag_loaded = True.
_rag_loaded = False
_rag_retrieve: Callable[..., Any] | None = None


def _load_retrieve() -> Callable[..., Any] | None:
    global _rag_loaded, _rag_retrieve
    if not _rag_loaded:
        try:
            from rag.retrieval import retrieve as _retrieve
        except ImportError:
            _rag_retrieve = None
        else:
            _rag_retrieve = _retrieve
        _rag_loaded = True
    return _rag_retrieve


def _always_available() -> bool:
    return True


def kb_search_available() -> bool:
    """True when the RAG package (rag.retrieval.retrieve) is importable."""
    try:
        return _load_retrieve() is not None
    except Exception:  # availability must never break registry construction
        return False


def web_search_available() -> bool:
    """True when Google Programmable Search keys are configured."""
    try:
        return default_web_search_client() is not None
    except Exception:
        return False


async def wafer_map_analyze(
    user: Any,
    *,
    csv_content: str | None = None,
    path: str | None = None,
    **_ignored: Any,
) -> Block:
    """Analyze a wafer-bin CSV (columns wafer_id,x,y,bin) into a wafer_map block.

    Accepts the CSV text directly (``csv_content``) or a filesystem path to a
    previously uploaded CSV (``path``). Never raises: malformed input returns
    the structured error block.
    """
    if isinstance(csv_content, str) and csv_content.strip():
        return wafer_map.analyze_wafer_csv(csv_content)
    if path is not None:
        try:
            content = Path(path).read_text(encoding='utf-8', errors='replace')
        except OSError as exc:
            return wafer_map.error_block(
                f'could not read the wafer CSV at {path!r}: {exc}',
                code='wafer_csv_unreadable',
            )
        return wafer_map.analyze_wafer_csv(content)
    return wafer_map.error_block(
        'no wafer CSV provided: pass csv_content (the CSV text) or path (a file)'
    )


async def excursion_triage(
    user: Any, *, metrics: Mapping[str, Any] | str | None = None, **_ignored: Any
) -> Block:
    """Run lot-level triage rules over lot metrics into a table block.

    ``metrics`` is a mapping (or a JSON object string, as an agent may pass
    one) of lot metrics; see :mod:`fabtools.excursion` for the rule set.
    Never raises: invalid metrics return the structured error block.
    """
    if metrics is None:
        return excursion.error_block(
            "no lot metrics provided: pass metrics={'yield_pct': ...}"
        )
    if isinstance(metrics, str):
        try:
            decoded = json.loads(metrics)
        except ValueError:
            return excursion.error_block('metrics must be a JSON object string or a mapping')
        metrics = decoded
    if not isinstance(metrics, Mapping):
        return excursion.error_block(
            f'lot metrics must be a mapping of names to numbers, got {type(metrics).__name__}'
        )
    return excursion.triage_lot(metrics)


async def spc_rules_check(
    user: Any,
    *,
    series: Any = None,
    sigma: Any = None,
    **kwargs: Any,
) -> Block:
    """Check a measurement series against control limits and the Nelson rules.

    ``series`` is the measurement text (CSV/JSON, 2-1000 numbers) or an
    already-parsed list; ``sigma`` is the optional known process sigma.
    Deterministic and always available. The model is not guided by a
    parameter schema, so a few common argument names are tolerated before
    declaring the input missing. Never raises: invalid input returns the
    structured error block.
    """
    if series is None:
        for alias in ('values', 'measurements', 'data', 'series_text'):
            if kwargs.get(alias) is not None:
                series = kwargs[alias]
                break
    if series is None:
        return spc.error_block(
            'no measurement series provided: pass series (CSV or JSON numbers, '
            '2-1000 points) and optionally sigma (the known process sigma)'
        )
    return spc.check_series(series, sigma)


async def kb_search(
    user: Any, *, query: str, k: int = 5, **_ignored: Any
) -> Block:
    """Search the user's knowledge base via rag.retrieval.retrieve.

    Returns a table block whose ``sources`` items follow the contract shape
    {document_id, chunk_id, title, snippet, score}. Unavailable (structured
    error block) when the RAG package is missing.
    """
    retrieve = _load_retrieve()
    if retrieve is None:
        return {
            'type': 'error',
            'title': 'Knowledge-base search unavailable',
            'code': 'kb_search_unavailable',
            'detail': 'the RAG module (rag.retrieval) is not installed',
            'rows': [],
            'sources': [],
        }
    if not query or not str(query).strip():
        return {
            'type': 'error',
            'title': 'Knowledge-base search failed',
            'code': 'kb_search_query_missing',
            'detail': 'no search query provided',
            'rows': [],
            'sources': [],
        }
    try:
        result = retrieve(user, str(query), k=k)
        if inspect.isawaitable(result):  # tolerate sync or async retrieve
            result = await result
    except Exception as exc:  # tool boundary: report, never raise
        return {
            'type': 'error',
            'title': 'Knowledge-base search failed',
            'code': 'kb_search_failed',
            'detail': f'{type(exc).__name__}: {exc}',
            'rows': [],
            'sources': [],
        }

    items = result if isinstance(result, list) else []
    rows: list[list[Any]] = []
    sources: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, Mapping):
            entry = dict(item)
        else:  # non-mapping chunk: keep it visible rather than dropping it
            entry = {
                'document_id': None,
                'chunk_id': None,
                'title': str(item),
                'snippet': '',
                'score': None,
            }
        sources.append(entry)
        rows.append([entry.get('title', ''), entry.get('snippet', ''), entry.get('score')])
    return {
        'type': 'table',
        'title': 'Knowledge-base search',
        'summary': f'{len(rows)} chunk(s) retrieved for {str(query)!r}',
        'columns': ['Document', 'Snippet', 'Score'],
        'rows': rows,
        'sources': sources,
    }


async def web_search(
    user: Any, *, query: str, k: int = 5, client: WebSearchClient | None = None, **_ignored: Any
) -> Block:
    """Search the web via an injectable client; degrades visibly without config.

    Without configured keys (and no injected client) this returns a
    structured error block explaining what to set — it never raises and
    never pretends to have searched.
    """
    if not query or not str(query).strip():
        return {
            'type': 'error',
            'title': 'Web search failed',
            'code': 'web_search_query_missing',
            'detail': 'no search query provided',
            'rows': [],
            'results': [],
        }
    resolved = client if client is not None else default_web_search_client()
    if resolved is None:
        return {
            'type': 'error',
            'title': 'Web search unavailable',
            'code': 'web_search_not_configured',
            'detail': (
                'web search needs GOOGLE_API_KEY and GOOGLE_SEARCH_ENGINE_ID '
                '(Google Programmable Search); set them in the environment or .env'
            ),
            'rows': [],
            'results': [],
        }
    try:
        results = await resolved.search(str(query), k=k)
    except Exception as exc:  # tool boundary: report, never raise
        return {
            'type': 'error',
            'title': 'Web search failed',
            'code': 'web_search_failed',
            'detail': f'{type(exc).__name__}: {exc}',
            'rows': [],
            'results': [],
        }

    rows: list[list[str]] = []
    clean: list[dict[str, str]] = []
    for item in results:
        if not isinstance(item, Mapping):
            continue
        entry = {
            'title': str(item.get('title', '')),
            'url': str(item.get('url', '')),
            'snippet': str(item.get('snippet', '')),
        }
        clean.append(entry)
        rows.append([entry['title'], entry['url'], entry['snippet']])
    return {
        'type': 'table',
        'title': 'Web search',
        'summary': f'{len(rows)} result(s) for {str(query)!r}',
        'columns': ['Title', 'Link', 'Snippet'],
        'rows': rows,
        'results': clean,
    }


TOOLS: list[tuple[str, ToolFn, AvailabilityFn]] = [
    ('wafer_map_analyze', wafer_map_analyze, _always_available),
    ('excursion_triage', excursion_triage, _always_available),
    ('spc_rules_check', spc_rules_check, _always_available),
    ('kb_search', kb_search, kb_search_available),
    ('web_search', web_search, web_search_available),
]
