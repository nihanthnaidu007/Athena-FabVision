"""Injectable web-search client for the ``web_search`` tool (OpenAI-free).

The default implementation calls the Google Programmable Search JSON API
(the spec's web-search provider) over HTTP; nothing in this module imports
OpenAI. ``WebSearchClient`` is a structural protocol so tests (and future
providers) can inject any object with the same ``search`` signature — no
test ever performs a real HTTP call. The client raises
:class:`WebSearchError` on backend failures; the tool layer in
:mod:`fabtools.tools` converts that into a structured error block.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, TypedDict

CSE_ENDPOINT = 'https://www.googleapis.com/customsearch/v1'
CSE_MAX_RESULTS = 10  # Google Programmable Search caps num at 10

Transport = Callable[[str, Mapping[str, str], float], Awaitable[dict[str, Any]]]


class WebSearchError(RuntimeError):
    """A web-search backend failed; the tool layer turns this into a block."""


class WebResult(TypedDict):
    title: str
    url: str
    snippet: str


class WebSearchClient(Protocol):
    """Anything with this async signature can serve as the web-search backend."""

    async def search(self, query: str, k: int = 5) -> list[WebResult]: ...


def _parse_cse_response(payload: Mapping[str, Any], k: int) -> list[WebResult]:
    """Map a Google Programmable Search payload to WebResult dicts (pure)."""
    error = payload.get('error')
    if isinstance(error, Mapping):
        message = error.get('message', 'unknown error')
        raise WebSearchError(f'Google Programmable Search error: {message}')
    items = payload.get('items') or []
    results: list[WebResult] = []
    for item in items[: max(k, 0)]:
        if not isinstance(item, Mapping):
            continue
        results.append({
            'title': str(item.get('title') or ''),
            'url': str(item.get('link') or ''),
            'snippet': str(item.get('snippet') or ''),
        })
    return results


async def _default_transport(
    url: str, params: Mapping[str, str], timeout_seconds: float
) -> dict[str, Any]:
    import aiohttp  # imported lazily to keep this module import-light

    async with aiohttp.ClientSession() as session:
        async with session.get(
            url,
            params=dict(params),
            timeout=aiohttp.ClientTimeout(total=timeout_seconds),
        ) as response:
            if response.status != 200:
                body = await response.text()
                raise WebSearchError(
                    f'Google Programmable Search returned HTTP {response.status}: {body[:200]}'
                )
            payload = await response.json(content_type=None)
    if not isinstance(payload, dict):
        raise WebSearchError('Google Programmable Search returned an unexpected payload')
    return payload


class GoogleCSEClient:
    """Google Programmable Search client with an injectable HTTP transport."""

    def __init__(
        self,
        api_key: str,
        engine_id: str,
        *,
        timeout_seconds: float = 10.0,
        transport: Transport | None = None,
    ):
        self._api_key = api_key
        self._engine_id = engine_id
        self._timeout_seconds = timeout_seconds
        self._transport: Transport = transport or _default_transport

    async def search(self, query: str, k: int = 5) -> list[WebResult]:
        payload = await self._transport(
            CSE_ENDPOINT,
            {
                'key': self._api_key,
                'cx': self._engine_id,
                'q': query,
                'num': str(min(max(k, 1), CSE_MAX_RESULTS)),
            },
            self._timeout_seconds,
        )
        return _parse_cse_response(payload, k)


def default_web_search_client() -> GoogleCSEClient | None:
    """Build the default client from settings; None when not configured.

    Keys are read lazily through ``django_agent.config`` so that importing
    ``fabtools`` never requires Django settings or any API key (zero-key
    boot stays clean), and an unconfigured environment simply degrades.
    """
    try:
        from django_agent.config import optional_var
    except ImportError:  # pragma: no cover - only reachable outside the app
        return None
    api_key = (optional_var('GOOGLE_API_KEY') or '').strip()
    engine_id = (optional_var('GOOGLE_SEARCH_ENGINE_ID') or '').strip()
    if not api_key or not engine_id:
        return None
    return GoogleCSEClient(api_key, engine_id)
