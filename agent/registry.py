"""Availability-aware tool registry and integration-contract loader.

A tool is an async callable ``(user, **kwargs) -> dict`` whose result is
a UI block ``{"type": "table" | "text" | "wafer_map" | "error", ...}``.
Every registration carries an optional availability callable; only
tools whose availability is satisfied are exposed to the model, so a
missing integration key disables a feature instead of failing a turn.

Integration contract (the five v1.0 PRs merge in any order):

* ``fabtools.tools.TOOLS`` -- list of ``[name, fn, availability_fn]``
  entries, loaded here with a defensive import.
* ``rag.retrieval.retrieve(user, query, k=5)`` -- the retrieval source,
  wired here; may be sync or async.

A missing module leaves that feature unavailable -- never a crash.
"""

from __future__ import annotations

import importlib
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger(__name__)

ToolFn = Callable[..., Awaitable[dict[str, Any]] | dict[str, Any]]
AvailabilityFn = Callable[[], bool]
RetrieveFn = Callable[..., Any]

BLOCK_TYPES = ('table', 'text', 'wafer_map', 'error')


async def maybe_await(value: Any) -> Any:
    """Await ``value`` when it is awaitable; pass anything else through."""
    if inspect.isawaitable(value):
        return await value
    return value


def normalize_source(item: Any) -> dict[str, Any]:
    """Coerce a retrieval hit into the canonical source-item shape.

    The contract shape is ``{document_id, chunk_id, title, snippet,
    score}``; malformed hits degrade to a titled, low-confidence entry
    rather than breaking a turn.
    """
    if not isinstance(item, dict):
        item = {'snippet': str(item)}
    try:
        score = float(item.get('score') or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    try:
        document_id = int(item.get('document_id') or 0)
        chunk_id = int(item.get('chunk_id') or 0)
    except (TypeError, ValueError):
        document_id = chunk_id = 0
    return {
        'document_id': document_id,
        'chunk_id': chunk_id,
        'title': str(item.get('title') or 'Untitled document'),
        'snippet': str(item.get('snippet') or ''),
        'score': score,
    }


class ToolRegistry:
    """Named tool registry with per-tool availability and one retrieval source."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolFn] = {}
        self._availability: dict[str, AvailabilityFn | None] = {}
        self._schemas: dict[str, dict[str, Any]] = {}
        self.retrieval: RetrieveFn | None = None

    def register(
        self,
        name: str,
        fn: ToolFn,
        availability_fn: AvailabilityFn | None = None,
        *,
        description: str = '',
        parameters: dict[str, Any] | None = None,
    ) -> None:
        """Register a tool; ``availability_fn`` returning False hides it.

        Registering an existing name replaces it -- the last registration
        wins, which lets tests shadow tools deterministically.
        """
        self._tools[name] = fn
        self._availability[name] = availability_fn
        self._schemas[name] = {
            'type': 'function',
            'function': {
                'name': name,
                'description': description or f'Tool {name}.',
                'parameters': parameters or {'type': 'object', 'properties': {}},
            },
        }

    def is_available(self, name: str) -> bool:
        """False for unknown names or tools whose availability fails."""
        fn = self._tools.get(name)
        if fn is None:
            return False
        availability_fn = self._availability.get(name)
        if availability_fn is None:
            return True
        try:
            return bool(availability_fn())
        except Exception:
            logger.exception('Availability check for tool %r failed; marking unavailable.', name)
            return False

    def tool_names(self) -> list[str]:
        """Names of exposed (available) tools only."""
        return [name for name in self._tools if self.is_available(name)]

    def tool_schemas(self) -> list[dict[str, Any]]:
        """OpenAI function-tool schemas for exposed tools only."""
        return [self._schemas[name] for name in self.tool_names()]

    async def execute(self, name: str, user: Any, **kwargs: Any) -> dict[str, Any]:
        """Run a tool and always answer a block -- never raise.

        Unknown/unavailable tools, exceptions, and non-dict results all
        become ``{"type": "error"}`` blocks so the model can react and
        the stream survives.
        """
        block = await self._run(name, user, **kwargs)
        if not isinstance(block, dict):
            logger.error('Tool %r returned %s instead of a block dict.', name, type(block).__name__)
            return _error_block(name, 'Tool returned an invalid result shape.')
        block_type = block.get('type')
        if block_type not in BLOCK_TYPES:
            return _error_block(name, f"Tool returned an invalid block type {block_type!r}.")
        return block

    async def _run(self, name: str, user: Any, **kwargs: Any) -> dict[str, Any]:
        if not self.is_available(name):
            return _error_block(name, f"Tool '{name}' is not available.")
        fn = self._tools[name]
        try:
            return await maybe_await(fn(user, **kwargs))
        except Exception as exc:
            logger.exception('Tool %r raised; converting to an error block.', name)
            return _error_block(name, str(exc))


def _error_block(name: str, message: str) -> dict[str, Any]:
    return {'type': 'error', 'title': f'Tool {name} failed', 'error': message}


#: The process-wide registry the SSE view and other consumers use.
REGISTRY = ToolRegistry()


def load_fab_tools(registry: ToolRegistry | None = None, importer: Any = None) -> bool:
    """Register every entry in ``fabtools.tools.TOOLS``.

    Returns True when tools were loaded. A missing module, malformed
    ``TOOLS``, or malformed entry is logged and skipped -- the feature
    simply stays unavailable.
    """
    reg = registry if registry is not None else REGISTRY
    import_module = importer or importlib.import_module
    try:
        module = import_module('fabtools.tools')
    except ImportError:
        logger.info('fabtools.tools is not installed; fab tools stay disabled.')
        return False

    entries = getattr(module, 'TOOLS', None)
    if not isinstance(entries, (list, tuple)):
        logger.warning(
            'fabtools.tools.TOOLS is %s, expected a list; fab tools stay disabled.',
            type(entries).__name__,
        )
        return False

    loaded = 0
    schemas = getattr(module, 'TOOL_SCHEMAS', None)
    if not isinstance(schemas, dict):
        schemas = {}
    for entry in entries:
        registered = _register_entry(reg, entry, schemas)
        loaded += 1 if registered else 0
    logger.info('Loaded %d of %d fab tool entries.', loaded, len(entries))
    return loaded > 0


def _register_entry(
    reg: ToolRegistry, entry: Any, schemas: dict[str, Any] | None = None
) -> bool:
    """Register one ``[name, fn, availability_fn]`` entry, defensively.

    An optional ``TOOL_SCHEMAS[name]`` entry supplies the LLM-facing
    description and parameter schema; a tool without one (or with a
    malformed one) keeps the generic schema, so schemas are additive.
    """
    if not isinstance(entry, (list, tuple)) or not 2 <= len(entry) <= 3:
        logger.warning('Skipping malformed fab tool entry: %r', entry)
        return False
    name, fn = entry[0], entry[1]
    availability_fn = entry[2] if len(entry) == 3 else None
    if not isinstance(name, str) or not callable(fn):
        logger.warning('Skipping malformed fab tool entry: %r', entry)
        return False
    if availability_fn is not None and not callable(availability_fn):
        logger.warning('Skipping fab tool %r: availability_fn is not callable.', name)
        return False
    schema = (schemas or {}).get(name)
    if not isinstance(schema, dict):
        schema = {}
    parameters = schema.get('parameters')
    if not isinstance(parameters, dict):
        parameters = None
    reg.register(
        name,
        fn,
        availability_fn,
        description=schema.get('description') or '',
        parameters=parameters,
    )
    return True


def wire_retrieval(registry: ToolRegistry | None = None, importer: Any = None) -> bool:
    """Wire ``rag.retrieval.retrieve`` as the registry's retrieval source.

    Returns True when retrieval is available. A missing module leaves
    retrieval off -- the agent answers without KB context.
    """
    reg = registry if registry is not None else REGISTRY
    import_module = importer or importlib.import_module
    try:
        module = import_module('rag.retrieval')
    except ImportError:
        logger.info('rag.retrieval is not installed; retrieval stays disabled.')
        return False
    retrieve = getattr(module, 'retrieve', None)
    if not callable(retrieve):
        logger.warning('rag.retrieval has no callable retrieve; retrieval stays disabled.')
        return False
    reg.retrieval = retrieve
    return True
