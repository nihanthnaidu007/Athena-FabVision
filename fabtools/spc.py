"""Deterministic SPC rules checking over a single measurement series.

The tool answers the engineer's question "is this alarm real?" without an
LLM: eight Nelson (WECO) run rules plus individuals-chart control limits,
computed with NumPy, in the exact shape of the wafer analyzer.

Limits follow the standard individuals (I-chart) convention for a single
series: the center line is the series mean and sigma is estimated as the
mean moving range divided by d2 (1.128 for subgroups of 2). A known
process sigma may be passed instead (``sigma=``), which makes the limits
exact rather than self-referential.

Boundary conventions (pinned by golden tests): "beyond k sigma" is
strictly outside (a point exactly on a limit is in control); "within
1 sigma" is strictly inside (a point exactly on 1 sigma breaks rule 7).

The eight Nelson rules (Nelson, L.S. (1984), "The Shewhart Control
Chart -- Tests for Special Causes", Journal of Quality Technology 16(4);
zones per the Western Electric Statistical Quality Control Handbook):

1. one point beyond 3 sigma from the center line
2. nine points in a row on the same side of the center line
3. six points in a row steadily increasing or decreasing
4. fourteen points in a row alternating up and down
5. two out of three consecutive points beyond 2 sigma, same side
6. four out of five consecutive points beyond 1 sigma, same side
7. fifteen points in a row within 1 sigma of the center line
8. eight points in a row beyond 1 sigma on either side

Rules whose window exceeds the series length are reported in ``issues``
as not evaluated rather than silently scored as passable. A series with
zero variation makes the limits degenerate; limit-based rules are then
skipped with an honest note. Malformed input never raises to the caller:
every failure returns the structured ``{"type": "error"}`` block naming
what was wrong.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

import numpy as np

#: Individuals-chart estimator: mean moving range / d2, d2 for n=2 (ASTM).
D2_SUBGROUP_TWO = 1.128

MIN_SERIES_POINTS = 2
MAX_SERIES_POINTS = 1000

#: Nelson rule windows in points (and count thresholds for rules 5 and 6).
NELSON_WINDOWS = {1: 1, 2: 9, 3: 6, 4: 14, 5: 3, 6: 5, 7: 15, 8: 8}
NELSON_COUNTS = {5: 2, 6: 4}

NELSON_DEFINITIONS = {
    1: '1 point beyond 3σ from the center line',
    2: '9 points in a row on the same side of the center line',
    3: '6 points in a row steadily increasing or decreasing',
    4: '14 points in a row alternating up and down',
    5: '2 of 3 consecutive points beyond 2σ on the same side',
    6: '4 of 5 consecutive points beyond 1σ on the same side',
    7: '15 points in a row within 1σ of the center line',
    8: '8 points in a row beyond 1σ on either side',
}

#: Canonical literature the rule definitions come from; always attached so
#: the verdict's explanation stays citable even with an empty knowledge base.
NELSON_REFERENCES = [
    'Nelson, L.S. (1984). "The Shewhart Control Chart — Tests for Special Causes". '
    'Journal of Quality Technology 16(4), pp. 237-239.',
    'Western Electric Company (1956). Statistical Quality Control Handbook, '
    'Section 3 (zone tests for lack of control).',
]

#: Knowledge-base lookup query used for the citation layer (tools wrapper).
KB_CITATION_QUERY = (
    'Nelson rules Western Electric zone tests control chart special cause variation SPC'
)

ErrorBlock = dict[str, Any]

# Round block numerics so payloads are stable across otherwise-identical runs.
_ROUND = 6


class SpcInputError(ValueError):
    """Strict-validation failure carrying a human-readable message."""


def error_block(detail: str, *, code: str = 'spc_input_invalid') -> ErrorBlock:
    """Build the structured error block the tool contract requires on bad input."""
    return {
        'type': 'error',
        'title': 'SPC rules check failed',
        'code': code,
        'detail': detail,
        'issues': [],
    }


def parse_series(value: Any) -> list[float]:
    """Coerce the pasted series into floats with strict validation.

    Accepts a list/tuple of numbers, a JSON array string, or a string of
    comma / semicolon / whitespace-separated numbers. Raises
    :class:`SpcInputError` naming the offending token on any violation.
    """
    if value is None:
        raise SpcInputError(
            'no measurement series provided: pass series as a list of numbers, '
            'a JSON array string, or comma/newline-separated numbers'
        )
    if isinstance(value, str):
        value = _parse_series_text(value)
    if not isinstance(value, (list, tuple)):
        raise SpcInputError(
            f'the series must be a list of numbers, got {type(value).__name__}'
        )

    values: list[float] = []
    for index, item in enumerate(value, start=1):
        if isinstance(item, bool):
            raise SpcInputError(f'point {index}: booleans are not measurements')
        if isinstance(item, (int, float)):
            number = float(item)
        elif isinstance(item, str):
            try:
                number = float(item.strip())
            except ValueError:
                raise SpcInputError(f'point {index}: {item.strip()!r} is not a number') from None
        else:
            raise SpcInputError(
                f'point {index}: expected a number, got {type(item).__name__}'
            )
        if not math.isfinite(number):
            raise SpcInputError(f'point {index}: {item!r} is not a finite measurement')
        values.append(number)

    if len(values) < MIN_SERIES_POINTS:
        raise SpcInputError(
            f'a control chart needs at least {MIN_SERIES_POINTS} points '
            f'(limits require a moving range), got {len(values)}'
        )
    if len(values) > MAX_SERIES_POINTS:
        raise SpcInputError(
            f'the series has {len(values)} points; at most {MAX_SERIES_POINTS} — '
            'split it into overlapping windows and check each'
        )
    return values


def _parse_series_text(text: str) -> list[Any]:
    """Parse pasted text: a JSON array first, else a number list."""
    stripped = text.strip()
    if not stripped:
        raise SpcInputError('the series is empty')
    if stripped.startswith('['):
        try:
            decoded = json.loads(stripped)
        except ValueError:
            raise SpcInputError(
                'the series looks like JSON but could not be parsed as an array of numbers'
            ) from None
        if not isinstance(decoded, list):
            raise SpcInputError('a JSON series must be an array of numbers')
        return decoded
    tokens = [token for token in re.split(r'[,;\s]+', stripped) if token]
    if not tokens:
        raise SpcInputError('the series is empty')
    return tokens


def _validate_sigma(sigma: Any) -> float | None:
    """Coerce the optional known process sigma; None means estimate it."""
    if sigma is None:
        return None
    if isinstance(sigma, str):
        try:
            sigma = float(sigma.strip())
        except ValueError:
            raise SpcInputError(f'sigma must be a positive number, got {sigma!r}') from None
    if isinstance(sigma, bool) or not isinstance(sigma, (int, float)):
        raise SpcInputError(f'sigma must be a positive number, got {type(sigma).__name__}')
    sigma = float(sigma)
    if not math.isfinite(sigma) or sigma <= 0:
        raise SpcInputError(f'sigma must be a positive, finite number, got {sigma!r}')
    return sigma


def _mean_moving_range(values: np.ndarray) -> float:
    """Mean of absolute successive differences (the I-chart sigma estimator)."""
    if values.size < MIN_SERIES_POINTS:
        raise SpcInputError(
            f'a moving range needs at least {MIN_SERIES_POINTS} points, got {values.size}'
        )
    return float(np.mean(np.abs(np.diff(values))))


def _point_phrase(index: int, value: float, z: float | None) -> str:
    location = f' at {format(value, ".6g")}'
    sigma_part = '' if z is None else f' ({z:+.2f}σ)'
    return f'point {index}{location}{sigma_part}'


def _runs_of(flags: list[int], min_length: int) -> list[tuple[int, int]]:
    """Maximal runs of equal nonzero flags as (start, length), length >= min."""
    runs: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(flags) + 1):
        if index == len(flags) or flags[index] != flags[start]:
            if flags[start] != 0 and index - start >= min_length:
                runs.append((start, index - start))
            start = index
    return runs


def _alternation_runs(diff_signs: list[int], min_points: int) -> list[tuple[int, int]]:
    """Maximal strictly-alternating nonzero diff-sign runs as (start, diffs).

    A run of ``d`` alternating diffs spans ``d + 1`` points; returns runs
    with ``d + 1 >= min_points``.
    """
    runs: list[tuple[int, int]] = []
    start = 0
    for index in range(1, len(diff_signs) + 1):
        continues = (
            index < len(diff_signs)
            and diff_signs[index] != 0
            and diff_signs[index] == -diff_signs[index - 1]
        )
        if not continues:
            length = index - start
            if diff_signs[start] != 0 and length + 1 >= min_points:
                runs.append((start, length))
            start = index
    return runs


def _side_windows(
    z: list[float], window: int, min_count: int, threshold: float, side: int
) -> list[tuple[int, list[int]]]:
    """Windows of ``window`` points holding >= ``min_count`` beyond-``threshold``σ points.

    ``side`` is +1 (upper) or -1 (lower); a point counts when
    ``side * z[i] > threshold``. Returns (start, beyond_indices).
    """
    hits: list[tuple[int, list[int]]] = []
    for start in range(len(z) - window + 1):
        beyond = [
            start + offset
            for offset in range(window)
            if side * z[start + offset] > threshold
        ]
        if len(beyond) >= min_count:
            hits.append((start, beyond))
    return hits


def _merge_windows(hits: list[tuple[int, list[int]]]) -> list[list[int]]:
    """Merge overlapping same-side windows into sorted point regions."""
    regions: list[list[int]] = []
    for start, beyond in hits:
        if regions and start <= regions[-1][-1]:
            regions[-1].extend(point for point in beyond if point not in regions[-1])
        else:
            regions.append(list(beyond))
    return regions


def analyze_series(values: list[float], sigma: float | None = None) -> dict[str, Any]:
    """Compute the ``spc_chart`` block from a validated series. Pure and deterministic.

    Raises :class:`SpcInputError` on invalid input; :func:`check_series` is
    the tool-boundary entry point that converts that into the error block.
    """
    values = parse_series(values)
    known_sigma = _validate_sigma(sigma)
    raw = np.asarray(values, dtype=float)

    mean = float(np.mean(raw))
    if known_sigma is not None:
        sigma_value = known_sigma
        sigma_source = 'provided'
    else:
        sigma_value = _mean_moving_range(raw) / D2_SUBGROUP_TWO
        sigma_source = 'estimated_from_moving_range'

    issues: list[str] = []
    if sigma_value == 0.0:
        issues.append(
            'the series has no variation, so the control limits are degenerate; '
            'limit-based rules 1, 5, 6, 7, and 8 were not evaluated'
        )

    ucl = mean + 3 * sigma_value
    lcl = mean - 3 * sigma_value

    violations: list[dict[str, Any]] = []
    if sigma_value > 0.0:
        z = [float(value) for value in (raw - mean) / sigma_value]
        violations = _nelson_violations(z, values, issues)
    else:
        z = []
        # Run rules stay evaluable in principle; on a constant series they
        # cannot fire (no side, no monotonic trend, no alternation), which
        # the empty result below states honestly rather than pretending.

    violations.sort(key=lambda entry: entry['points'][0])
    point_flags: list[list[str]] = [[] for _ in values]
    for entry in violations:
        for point in entry['points']:
            point_flags[point - 1].append(entry['rule'])

    verdict = 'out_of_control' if violations else 'in_control'
    if not violations and sigma_value == 0.0:
        verdict = 'indeterminate'
    return {
        'type': 'spc_chart',
        'title': 'SPC control-chart check',
        'summary': _verdict(violations, issues, sigma_value),
        'verdict': verdict,
        'series_length': len(values),
        'sigma_source': sigma_source,
        'mean': round(mean, _ROUND),
        'sigma': round(sigma_value, _ROUND),
        'ucl': round(ucl, _ROUND),
        'lcl': round(lcl, _ROUND),
        'values': [round(value, _ROUND) for value in values],
        'violations': violations,
        'point_flags': point_flags,
        'references': list(NELSON_REFERENCES),
        'issues': issues,
    }


def _verdict(
    violations: list[dict[str, Any]], issues: list[str], sigma_value: float
) -> str:
    """Plain-language verdict: the sentence the user reads first."""
    if sigma_value == 0.0:
        return (
            'Inconclusive: every reading is identical, so the control limits '
            'are degenerate and the rules cannot be judged.'
        )
    if violations:
        labels = sorted({entry['label'].split(' — ')[0] for entry in violations})
        return (
            f'Out of control: {len(violations)} Nelson rule violation(s) detected '
            f'({", ".join(labels)}). Special-cause variation is present; '
            'investigate the flagged points before trusting the process.'
        )
    if issues:
        return (
            'In control: no Nelson rule violations in the evaluated rules; '
            'some rules were not evaluated (see issues).'
        )
    return (
        'In control: no Nelson rule violations; the series is consistent with '
        'common-cause variation within the computed control limits.'
    )


def _nelson_violations(
    z: list[float], values: list[float], issues: list[str]
) -> list[dict[str, Any]]:
    """Evaluate the eight Nelson rules over standardized points.

    Rules whose window exceeds the series are noted in ``issues`` and not
    evaluated; each fired rule yields one entry per distinct region with
    1-based point indices.
    """
    n = len(z)
    violations: list[dict[str, Any]] = []

    for rule, window in NELSON_WINDOWS.items():
        if window > n:
            issues.append(
                f'Nelson rule {rule} not evaluated: it needs {window} points in a row, '
                f'the series has {n}'
            )
    evaluated = [rule for rule, window in NELSON_WINDOWS.items() if window <= n]

    def add(rule: int, points: list[int], detail: str) -> None:
        label = f'Nelson rule {rule} — {NELSON_DEFINITIONS[rule]}'
        violations.append({
            'rule': f'nelson_{rule}',
            'label': label,
            'points': points,
            'detail': detail,
        })

    # Rule 1: any point strictly beyond 3σ, either side.
    if 1 in evaluated:
        beyond = [i for i in range(1, n + 1) if abs(z[i - 1]) > 3]
        if beyond:
            detail = '; '.join(
                _point_phrase(i, values[i - 1], z[i - 1]) + ' is beyond 3σ'
                for i in beyond
            )
            add(1, beyond, detail)

    # Rule 2: nine in a row on one side of the center line.
    if 2 in evaluated:
        sides = [int(np.sign(z[i])) for i in range(n)]
        for start, length in _runs_of(sides, NELSON_WINDOWS[2]):
            points = list(range(start + 1, start + length + 1))
            side_text = 'above' if sides[start] > 0 else 'below'
            detail = (
                f'points {points[0]}–{points[-1]} are {length} in a row {side_text} '
                f'the center line ({format(values[start], ".6g")} … '
                f'{format(values[start + length - 1], ".6g")})'
            )
            add(2, points, detail)

    # Rule 3: six in a row steadily increasing or decreasing.
    if 3 in evaluated:
        diff_signs = [int(np.sign(z[i] - z[i - 1])) for i in range(1, n)]
        for start, length in _runs_of(diff_signs, NELSON_WINDOWS[3] - 1):
            points = list(range(start + 1, start + length + 2))
            direction = 'increasing' if diff_signs[start] > 0 else 'decreasing'
            detail = (
                f'points {points[0]}–{points[-1]} run {length + 1} in a row '
                f'steadily {direction} '
                f'({format(values[start], ".6g")} → '
                f'{format(values[start + length], ".6g")})'
            )
            add(3, points, detail)

    # Rule 4: fourteen in a row alternating up and down.
    if 4 in evaluated:
        diff_signs = [int(np.sign(z[i] - z[i - 1])) for i in range(1, n)]
        for start, length in _alternation_runs(diff_signs, NELSON_WINDOWS[4]):
            points = list(range(start + 1, start + length + 2))
            detail = (
                f'points {points[0]}–{points[-1]} alternate up and down for '
                f'{length + 1} points in a row'
            )
            add(4, points, detail)

    # Rules 5 and 6: k of m consecutive beyond a sigma line, same side.
    for rule in (5, 6):
        if rule not in evaluated:
            continue
        window = NELSON_WINDOWS[rule]
        min_count = NELSON_COUNTS[rule]
        threshold = 2 if rule == 5 else 1
        for side in (1, -1):
            hits = _side_windows(z, window, min_count, threshold, side)
            for region in _merge_windows(hits):
                side_text = 'above' if side > 0 else 'below'
                points = [point + 1 for point in region]
                detail = '; '.join(
                    _point_phrase(i, values[i - 1], z[i - 1])
                    + f' is beyond {threshold}σ {side_text} the center line'
                    for i in points
                )
                add(rule, points, detail)

    # Rule 7: fifteen in a row strictly within 1σ (stratification).
    if 7 in evaluated:
        within = [1 if abs(z[i]) < 1 else 0 for i in range(n)]
        for start, length in _runs_of(within, NELSON_WINDOWS[7]):
            points = list(range(start + 1, start + length + 1))
            detail = (
                f'points {points[0]}–{points[-1]} all sit within 1σ of the center '
                'line (stratification — the limits may not reflect the real process)'
            )
            add(7, points, detail)

    # Rule 8: eight in a row strictly beyond 1σ, either side (mixture).
    if 8 in evaluated:
        beyond = [1 if abs(z[i]) > 1 else 0 for i in range(n)]
        for start, length in _runs_of(beyond, NELSON_WINDOWS[8]):
            points = list(range(start + 1, start + length + 1))
            detail = (
                f'points {points[0]}–{points[-1]} all sit beyond 1σ from the center '
                'line on either side (mixture — two interacting process populations)'
            )
            add(8, points, detail)

    return violations


def check_series(series: Any, sigma: Any = None) -> dict[str, Any]:
    """Analyze a pasted series into an ``spc_chart`` block, or a structured error block.

    This is the tool-boundary entry point: it never raises to the caller.
    """
    try:
        return analyze_series(series, sigma=sigma)
    except SpcInputError as exc:
        return error_block(str(exc))
    except Exception as exc:  # tool boundary: report the failure, never raise
        return error_block(
            f'unexpected failure while checking the series: {type(exc).__name__}: {exc}',
            code='spc_analysis_failed',
        )
