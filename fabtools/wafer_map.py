"""Deterministic wafer-map analysis over strict wafer-bin CSV input.

Schema (strict): the header must contain the columns ``wafer_id``, ``x``,
``y`` and ``bin`` (case-insensitive; extra columns are ignored). Each data
row must carry a non-empty ``wafer_id``, integer coordinates, and a
non-negative integer bin code. Bin ``1`` is the pass bin (spec: Athena
FabVision v1.0); every other valid bin counts as a fail.

Analysis computes total dies, yield, the bin distribution, and two spatial
signals over the die grid:

- edge ring: fail rate in the outer 10% radius band minus the wafer-mean
  fail rate (spec: "radial binning: fail-rate in outer 10% radius vs wafer
  mean"),
- center hotspot: fail rate in the inner quarter-radius region minus the
  fail rate everywhere else.

A pattern is flagged only when its differential exceeds the threshold and
the region holds enough dies to mean anything (``MIN_RING_DIES`` /
``MIN_CENTER_DIES``); regions too small to evaluate are reported in
``issues`` rather than silently scored as zero.

Malformed input never raises to the caller: every failure returns the
structured ``{"type": "error"}`` block naming what was wrong and where.
"""

from __future__ import annotations

import csv
import io
import math
from typing import Any

REQUIRED_COLUMNS = ('wafer_id', 'x', 'y', 'bin')
PASS_BIN = 1

# Spatial-pattern thresholds: a pattern is flagged when its fail-rate
# differential (region minus baseline, in [0, 1]) exceeds this margin.
EDGE_RING_THRESHOLD = 0.15
CENTER_HOTSPOT_THRESHOLD = 0.15
EDGE_RING_RADIUS_FRACTION = 0.9  # outer band = outer 10% of the radius
CENTER_RADIUS_FRACTION = 0.25    # hotspot region = inner quarter radius
MIN_RING_DIES = 8                # below this the outer band is too sparse to judge
MIN_CENTER_DIES = 4

ErrorBlock = dict[str, Any]


class WaferCsvError(ValueError):
    """Strict-validation failure carrying a human-readable message."""


def error_block(detail: str, *, code: str = 'wafer_csv_invalid') -> ErrorBlock:
    """Build the structured error block the tool contract requires on bad input."""
    return {
        'type': 'error',
        'title': 'Wafer map analysis failed',
        'code': code,
        'detail': detail,
        'issues': [],
    }


def _is_int(text: str) -> bool:
    try:
        int(text)
    except ValueError:
        return False
    return True


def parse_wafer_csv(content: str) -> list[dict[str, Any]]:
    """Parse CSV text into rows of {wafer_id, x, y, bin} with strict validation.

    Raises :class:`WaferCsvError` with a row-level message on any schema or
    type violation; :func:`analyze_wafer_csv` converts that into the
    structured error block.
    """
    if not content or not content.strip():
        raise WaferCsvError('the CSV is empty')

    try:
        rows = list(csv.reader(io.StringIO(content.lstrip('\ufeff'))))
    except csv.Error as exc:
        raise WaferCsvError(f'the CSV could not be parsed: {exc}') from None

    if not rows or not any(cell.strip() for cell in rows[0]):
        raise WaferCsvError('the CSV has no header row')

    header = [cell.strip().lower() for cell in rows[0]]
    missing = [column for column in REQUIRED_COLUMNS if column not in header]
    if missing:
        raise WaferCsvError(f"missing required column(s): {', '.join(missing)}")
    if len(set(header)) != len(header):
        dupes = sorted({name for name in header if header.count(name) > 1})
        raise WaferCsvError(f'duplicate column(s): {", ".join(dupes)}')
    index = {column: header.index(column) for column in REQUIRED_COLUMNS}

    parsed: list[dict[str, Any]] = []
    for line_number, raw in enumerate(rows[1:], start=2):
        if not any(cell.strip() for cell in raw):
            continue  # tolerate blank padding lines at the file edges
        if len(raw) != len(header):
            raise WaferCsvError(
                f'row {line_number}: has {len(raw)} field(s), expected {len(header)}'
            )
        wafer_id = raw[index['wafer_id']].strip()
        if not wafer_id:
            raise WaferCsvError(f"row {line_number}: 'wafer_id' must not be empty")
        for column in ('x', 'y', 'bin'):
            value = raw[index[column]].strip()
            if not _is_int(value):
                raise WaferCsvError(
                    f"row {line_number}: column '{column}' must be an integer, got {value!r}"
                )
        bin_code = int(raw[index['bin']])
        if bin_code < 0:
            raise WaferCsvError(f"row {line_number}: 'bin' must be non-negative, got {bin_code}")
        parsed.append({
            'wafer_id': wafer_id,
            'x': int(raw[index['x']]),
            'y': int(raw[index['y']]),
            'bin': bin_code,
        })

    if not parsed:
        raise WaferCsvError('the CSV has a header but no data rows')
    return parsed


def _fail_rate(group: list[dict[str, Any]]) -> float:
    if not group:
        return 0.0
    failing = sum(1 for row in group if row['bin'] != PASS_BIN)
    return failing / len(group)


def _region_score(
    region: list[dict[str, Any]],
    baseline_fail_rate: float,
    *,
    label: str,
    min_dies: int,
    issues: list[str],
) -> float | None:
    """Region fail rate minus the given baseline; None if too small to judge."""
    if len(region) < min_dies:
        issues.append(
            f'{label} region has {len(region)} die(s), below the minimum of {min_dies}; '
            'score not evaluated'
        )
        return None
    return _fail_rate(region) - baseline_fail_rate


def analyze_wafer_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute the wafer_map block from parsed rows. Pure and deterministic."""
    total = len(rows)
    passes = sum(1 for row in rows if row['bin'] == PASS_BIN)
    yield_pct = passes / total

    center_x = (min(row['x'] for row in rows) + max(row['x'] for row in rows)) / 2
    center_y = (min(row['y'] for row in rows) + max(row['y'] for row in rows)) / 2
    for row in rows:
        row['distance'] = math.hypot(row['x'] - center_x, row['y'] - center_y)
    radius_max = max(row['distance'] for row in rows)

    issues: list[str] = []
    wafer_ids = sorted({row['wafer_id'] for row in rows})
    if len(wafer_ids) > 1:
        issues.append(
            f'multiple wafer_ids present ({len(wafer_ids)}): '
            f'{", ".join(wafer_ids)} — rows were analyzed together'
        )

    wafer_mean_fail_rate = (total - passes) / total

    outer = [row for row in rows if row['distance'] >= EDGE_RING_RADIUS_FRACTION * radius_max]
    inner = [row for row in rows if row['distance'] <= CENTER_RADIUS_FRACTION * radius_max]
    # Spec: edge ring is the outer-band fail rate *vs the wafer mean*.
    edge_ring_score = _region_score(
        outer, wafer_mean_fail_rate,
        label='outer edge-ring', min_dies=MIN_RING_DIES, issues=issues,
    )
    # Spec: center hotspot is the inner-region fail rate vs everywhere else.
    center_hotspot_score = _region_score(
        inner, _fail_rate([row for row in rows if row not in inner]),
        label='center hotspot', min_dies=MIN_CENTER_DIES, issues=issues,
    )

    patterns: list[str] = []
    if edge_ring_score is not None and edge_ring_score > EDGE_RING_THRESHOLD:
        patterns.append('edge_ring')
    if center_hotspot_score is not None and center_hotspot_score > CENTER_HOTSPOT_THRESHOLD:
        patterns.append('center_hotspot')

    bin_tallies: dict[int, int] = {}
    for row in rows:
        bin_tallies[row['bin']] = bin_tallies.get(row['bin'], 0) + 1
    bin_counts = {str(code): bin_tallies[code] for code in sorted(bin_tallies)}

    pattern_text = ', '.join(patterns) if patterns else 'none'
    return {
        'type': 'wafer_map',
        'title': 'Wafer map analysis',
        'summary': f'{passes}/{total} die pass (yield {yield_pct:.1%}); patterns: {pattern_text}',
        'total_dies': total,
        'pass_count': passes,
        'fail_count': total - passes,
        'yield_pct': round(yield_pct, 6),
        'bin_counts': bin_counts,
        'edge_ring_score': None if edge_ring_score is None else round(edge_ring_score, 4),
        'center_hotspot_score': (
            None if center_hotspot_score is None else round(center_hotspot_score, 4)
        ),
        'patterns': patterns,
        'issues': issues,
    }


def analyze_wafer_csv(content: str) -> dict[str, Any]:
    """Analyze CSV text into a ``wafer_map`` block, or a structured error block.

    This is the tool-boundary entry point: it never raises to the caller.
    """
    try:
        return analyze_wafer_rows(parse_wafer_csv(content))
    except WaferCsvError as exc:
        return error_block(str(exc))
    except Exception as exc:  # tool boundary: report the failure, never raise
        return error_block(
            f'unexpected failure while analyzing the wafer CSV: {type(exc).__name__}: {exc}',
            code='wafer_analysis_failed',
        )
