"""Pure aggregation helpers for the usage dashboard.

Everything here takes plain sequences of UsageEvent-like objects and
returns JSON-safe dicts -- no database, no request, no I/O -- so the
numbers the dashboard renders can be unit-tested without fixtures.
"""

import math
from datetime import timedelta

from django.utils import timezone

DEFAULT_RANGE = 7
ALLOWED_RANGES = (7, 30)


def parse_days(raw: str | None, allowed: tuple[int, ...] = ALLOWED_RANGES) -> int:
    """Return the selected window size; anything invalid falls back to the default.

    The selector is user input rendered into links, so the whitelist keeps
    unknown values from reaching the query layer.
    """
    try:
        value = int(raw) if raw is not None else DEFAULT_RANGE
    except (TypeError, ValueError):
        return DEFAULT_RANGE
    return value if value in allowed else DEFAULT_RANGE


def p95(values: list[int]) -> int:
    """Nearest-rank 95th percentile of non-negative ints (0 for no samples)."""
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, math.ceil(0.95 * len(ordered)))
    return ordered[rank - 1]


def daily_series(events, *, days: int, now) -> list[dict]:
    """Bucket events per local calendar day across the full window.

    Returns ``days`` entries in ascending date order -- including empty
    days, so charts and tables show gaps instead of silently compressing
    the timeline. Events outside the window are ignored.
    """
    start_date = timezone.localdate(now) - timedelta(days=days - 1)
    buckets: dict = {}
    for event in events:
        day = timezone.localdate(event.created_at)
        if not start_date <= day <= timezone.localdate(now):
            continue
        bucket = buckets.setdefault(
            day, {'calls': 0, 'tokens_in': 0, 'tokens_out': 0, 'latencies': []}
        )
        bucket['calls'] += 1
        bucket['tokens_in'] += event.tokens_in
        bucket['tokens_out'] += event.tokens_out
        bucket['latencies'].append(event.latency_ms)

    series = []
    for offset in range(days):
        day = start_date + timedelta(days=offset)
        bucket = buckets.get(day, {})
        latencies = bucket.get('latencies', [])
        series.append(
            {
                'date': day,
                'calls': bucket.get('calls', 0),
                'tokens_in': bucket.get('tokens_in', 0),
                'tokens_out': bucket.get('tokens_out', 0),
                'avg_latency_ms': round(sum(latencies) / len(latencies)) if latencies else 0,
                'p95_latency_ms': p95(latencies),
            }
        )
    return series


def usage_summary(events, *, days: int, now) -> dict:
    """Aggregate UsageEvent-like rows into the dashboard's summary shape.

    Every number -- totals, averages, percentiles -- comes from the same
    window-filtered event list, so nothing outside the window can leak
    into any aggregate.
    """
    end_date = timezone.localdate(now)
    start_date = end_date - timedelta(days=days - 1)
    in_window = [
        event for event in events if start_date <= timezone.localdate(event.created_at) <= end_date
    ]
    daily = daily_series(in_window, days=days, now=now)
    latencies = [event.latency_ms for event in in_window]
    return {
        'days': days,
        'total_calls': sum(day['calls'] for day in daily),
        'tokens_in': sum(day['tokens_in'] for day in daily),
        'tokens_out': sum(day['tokens_out'] for day in daily),
        'avg_latency_ms': round(sum(latencies) / len(latencies)) if latencies else 0,
        'p95_latency_ms': p95(latencies),
        'daily': daily,
    }


def chart_bars(daily, *, width=560, height=180, pad=24, label_pad=20) -> dict:
    """Map a daily series onto inline-SVG bar geometry (no chart library).

    Zero calls still yields a flat baseline of zero-height bars rather
    than a division by zero, and the template only ever reads numbers.
    """
    plot_h = height - pad - label_pad
    plot_w = width - 2 * pad
    max_calls = max((day['calls'] for day in daily), default=0) or 1
    slot = plot_w / len(daily) if daily else plot_w
    bar_w = slot * 0.6
    bars = []
    for index, day in enumerate(daily):
        bar_h = plot_h * day['calls'] / max_calls
        bars.append(
            {
                'x': round(pad + index * slot + (slot - bar_w) / 2, 1),
                'y': round(pad + plot_h - bar_h, 1),
                'w': round(bar_w, 1),
                'h': round(bar_h, 1),
                'label': day['date'].strftime('%b %d'),
                'value': day['calls'],
            }
        )
    return {
        'bars': bars,
        'width': width,
        'height': height,
        'pad': pad,
        'baseline_y': pad + plot_h,
        'max_calls': max_calls,
    }
