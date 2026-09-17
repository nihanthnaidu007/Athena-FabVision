"""Lot-level excursion triage: threshold rules with reasons, as a table block.

The tool takes a mapping of lot metrics and evaluates three documented rules
against module-level thresholds:

- ``yield_pct`` (required, 0-100): below ``YIELD_CRITICAL_BELOW`` is a
  critical flag, below ``YIELD_WATCH_BELOW`` a warning.
- ``defect_density`` (optional, >= 0, defects per cm^2): above
  ``DEFECT_DENSITY_CRITICAL_ABOVE`` is critical, above
  ``DEFECT_DENSITY_WARNING_ABOVE`` a warning.
- ``wafers_flagged_pct`` (optional, 0-100): at or above
  ``LOT_SPREAD_AT_LEAST`` the excursion is treated as lot-level.

Each fired rule becomes a table row (rule, severity, observed, threshold,
reason) plus a severity-ranked triage checklist. Unknown metrics are
ignored; missing required metrics and present-but-invalid metrics return
the structured ``{"type": "error"}`` block. Nothing here touches the
network or the database, and the function never raises to the caller.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

YIELD_CRITICAL_BELOW = 80.0
YIELD_WATCH_BELOW = 95.0
DEFECT_DENSITY_WARNING_ABOVE = 0.5
DEFECT_DENSITY_CRITICAL_ABOVE = 1.0
LOT_SPREAD_AT_LEAST = 30.0

ErrorBlock = dict[str, Any]
Flag = tuple[str, str, str, str, str]  # rule, severity, observed, threshold, reason


def error_block(detail: str, *, code: str = 'excursion_metrics_invalid') -> ErrorBlock:
    """Build the structured error block the tool contract requires on bad input."""
    return {
        'type': 'error',
        'title': 'Excursion triage failed',
        'code': code,
        'detail': detail,
        'rows': [],
        'checklist': [],
    }


def _metric(metrics: Mapping[str, Any], name: str, *, required: bool = False) -> float | None:
    """Read a numeric metric; None when absent, ValueError with a named reason."""
    if name not in metrics or metrics[name] is None:
        if required:
            raise ValueError(f"required metric '{name}' is missing")
        return None
    value = metrics[name]
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"metric '{name}' must be a number, got {value!r}") from None
    return number


def _bounded(value: float, low: float, high: float, name: str) -> None:
    if value < low or value > high:
        raise ValueError(f"metric '{name}' must be between {low:g} and {high:g}, got {value:g}")


def _yield_flag(yield_pct: float) -> Flag | None:
    if yield_pct < YIELD_CRITICAL_BELOW:
        return (
            'YIELD_CRITICAL', 'critical', f'{yield_pct:g}%', f'< {YIELD_CRITICAL_BELOW:g}%',
            'lot yield is below the critical line; contain the affected material',
        )
    if yield_pct < YIELD_WATCH_BELOW:
        return (
            'YIELD_WATCH', 'warning', f'{yield_pct:g}%', f'< {YIELD_WATCH_BELOW:g}%',
            'lot yield is below the watch line; review bin and parametric data',
        )
    return None


def _density_flag(density: float) -> Flag | None:
    if density > DEFECT_DENSITY_CRITICAL_ABOVE:
        return (
            'DEFECT_DENSITY_CRITICAL', 'critical', f'{density:g}/cm2',
            f'> {DEFECT_DENSITY_CRITICAL_ABOVE:g}',
            'defect density is critically high; suspect a particle or process event',
        )
    if density > DEFECT_DENSITY_WARNING_ABOVE:
        return (
            'DEFECT_DENSITY_HIGH', 'warning', f'{density:g}/cm2',
            f'> {DEFECT_DENSITY_WARNING_ABOVE:g}',
            'defect density is above the warning line; trend it against prior lots',
        )
    return None


def _spread_flag(share: float) -> Flag | None:
    if share >= LOT_SPREAD_AT_LEAST:
        return (
            'LOT_SPREAD', 'critical', f'{share:g}%', f'>= {LOT_SPREAD_AT_LEAST:g}%',
            'excursion affects a large share of the lot; treat as lot-level, not single-wafer',
        )
    return None


def _triage_checklist(critical: bool, warning: bool) -> list[str]:
    steps: list[str] = []
    if critical:
        steps += [
            'Quarantine: hold affected wafers and stop further shipments of the lot',
            'Contain: find the shared process step, chamber, or time window across flagged wafers',
            'Escalate: page the process owner for the suspect step with the flags attached',
        ]
    if warning:
        steps += [
            'Verify: re-check metrology and bin definitions behind the flagged metrics',
            'Compare: diff the flagged lot against sibling lots run in the same window',
        ]
    if not steps:
        steps = [
            'Record: file the metrics as a routine lot data point',
            'Monitor: watch the next lot for drift toward the watch lines',
        ]
    return steps


def triage_lot(metrics: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate the triage rules over lot metrics; pure and deterministic."""
    if not isinstance(metrics, Mapping):
        return error_block(
            f'lot metrics must be a mapping of names to numbers, got {type(metrics).__name__}'
        )

    try:
        yield_pct = _metric(metrics, 'yield_pct', required=True)
        assert yield_pct is not None  # required metrics always come back numeric
        _bounded(yield_pct, 0.0, 100.0, 'yield_pct')
        density = _metric(metrics, 'defect_density')
        if density is not None and density < 0:
            raise ValueError(f"metric 'defect_density' must be non-negative, got {density:g}")
        spread = _metric(metrics, 'wafers_flagged_pct')
        if spread is not None:
            _bounded(spread, 0.0, 100.0, 'wafers_flagged_pct')
    except ValueError as exc:
        return error_block(str(exc))

    candidates: list[Flag | None] = [
        _yield_flag(yield_pct),
        None if density is None else _density_flag(density),
        None if spread is None else _spread_flag(spread),
    ]
    flags = [flag for flag in candidates if flag is not None]
    severities = {flag[1] for flag in flags}
    checklist = _triage_checklist('critical' in severities, 'warning' in severities)
    rows = [[rule, severity, observed, threshold, reason]
            for rule, severity, observed, threshold, reason in flags]

    if flags:
        summary = f"{len(flags)} excursion flag(s): " + ', '.join(flag[0] for flag in flags)
    else:
        summary = 'no excursion flags; lot metrics are within thresholds'

    return {
        'type': 'table',
        'title': 'Excursion triage',
        'summary': summary,
        'columns': ['Rule', 'Severity', 'Observed', 'Threshold', 'Reason'],
        'rows': rows,
        'checklist': checklist,
        'flags': [flag[0] for flag in flags],
    }
