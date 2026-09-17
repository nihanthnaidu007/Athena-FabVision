"""Zero-network test doubles for the voice suite (no LiveKit server, no API).

Reuses the shared agent fakes (scripted LLM events) and adds a minimal
ToolRegistry double and a fake RTC room for reconnect-handler tests.
"""

from __future__ import annotations

from typing import Any

#: Full LiveKit config for flag-on tests (override_settings kwargs).
LIVEKIT_CONFIG = {
    'LIVEKIT_URL': 'wss://livekit.test',
    'LIVEKIT_API_KEY': 'test-api-key',
    'LIVEKIT_API_SECRET': 'test-livekit-secret-0123456789abcdef',
}


class FakeRegistry:
    """Minimal ToolRegistry double: named fake tools + scripted retrieval.

    Covers the surface the shared core and the voice adapters touch:
    ``retrieval``, ``tool_schemas``, ``tool_names``, ``is_available``,
    and ``execute`` (which returns the real error block for unknown or
    unavailable tools).
    """

    def __init__(self, tools: dict[str, Any] | None = None, retrieval: Any = None) -> None:
        self._tools = dict(tools or {})
        self.retrieval = retrieval
        self.executed: list[tuple[str, dict[str, Any]]] = []

    def tool_names(self) -> list[str]:
        return list(self._tools)

    def tool_schemas(self) -> list[dict[str, Any]]:
        return []

    def is_available(self, name: str) -> bool:
        return name in self._tools

    async def execute(self, name: str, user: Any, **kwargs: Any) -> dict[str, Any]:
        self.executed.append((name, kwargs))
        if name not in self._tools:
            # Mirror the real ToolRegistry error-block shape for unknown tools.
            return {
                'type': 'error',
                'title': f'Tool {name} failed',
                'error': f"Tool '{name}' is not available.",
            }
        result = self._tools[name](user=user, **kwargs)
        return await result if hasattr(result, '__await__') else result


class FakeRoom:
    """Records ``.on()`` registrations; handlers fire via ``emit()``."""

    def __init__(self, name: str = 'voice-user-1'):
        self.name = name
        self.handlers: dict[str, list[Any]] = {}

    def on(self, event: str):
        def register(fn):
            self.handlers.setdefault(event, []).append(fn)
            return fn

        return register

    def emit(self, event: str, *args: Any) -> None:
        for fn in self.handlers.get(event, []):
            fn(*args)
