"""Fab-analyst tool layer: wafer-map analysis, excursion triage, KB and web search.

The integration contract (Athena FabVision v1.0 spec): every tool is an async
callable ``(user, **kwargs)`` returning a block dict ``{type, title, ...}``
where ``type`` is one of ``table | text | wafer_map | error``. Consumers
import :mod:`fabtools.tools` defensively and gate on its availability
functions, so this package may merge before or after its siblings.
"""
