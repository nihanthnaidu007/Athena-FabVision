"""Unit tests for the pure dashboard aggregation helpers (no database)."""

from datetime import datetime
from datetime import timezone as dt_timezone
from types import SimpleNamespace

from django.test import TestCase

from dashboard.aggregates import chart_bars, daily_series, p95, parse_days, usage_summary

UTC = dt_timezone.utc
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def ev(*, created_at, tokens_in=0, tokens_out=0, latency_ms=0):
    """A UsageEvent-shaped stand-in; aggregates only need these attributes."""
    return SimpleNamespace(
        created_at=created_at,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
    )


class ParseDaysTests(TestCase):
    def test_none_and_invalid_fall_back_to_default(self):
        self.assertEqual(parse_days(None), 7)
        self.assertEqual(parse_days('abc'), 7)
        self.assertEqual(parse_days('-1'), 7)
        self.assertEqual(parse_days('11'), 7)

    def test_allowed_windows_pass_through(self):
        self.assertEqual(parse_days('7'), 7)
        self.assertEqual(parse_days('30'), 30)


class P95Tests(TestCase):
    def test_empty_and_single_value(self):
        self.assertEqual(p95([]), 0)
        self.assertEqual(p95([100]), 100)

    def test_nearest_rank_percentile(self):
        self.assertEqual(p95(list(range(1, 101))), 95)
        self.assertEqual(p95([10, 20, 30, 40]), 40)


class DailySeriesTests(TestCase):
    def test_buckets_by_day_and_fills_empty_days(self):
        events = [
            ev(
                created_at=datetime(2026, 9, 16, 1, 0, tzinfo=UTC),
                tokens_in=10,
                tokens_out=20,
                latency_ms=100,
            ),
            ev(
                created_at=datetime(2026, 9, 16, 5, 0, tzinfo=UTC),
                tokens_in=30,
                tokens_out=40,
                latency_ms=300,
            ),
            ev(created_at=datetime(2026, 9, 8, 5, 0, tzinfo=UTC), latency_ms=999),
        ]
        series = daily_series(events, days=7, now=NOW)
        self.assertEqual(len(series), 7)
        self.assertEqual(series[0]['date'].isoformat(), '2026-09-11')
        by_date = {day['date'].isoformat(): day for day in series}
        self.assertEqual(by_date['2026-09-16']['calls'], 2)
        self.assertEqual(by_date['2026-09-16']['tokens_in'], 40)
        self.assertEqual(by_date['2026-09-16']['tokens_out'], 60)
        self.assertEqual(by_date['2026-09-16']['avg_latency_ms'], 200)
        self.assertEqual(by_date['2026-09-16']['p95_latency_ms'], 300)
        self.assertEqual(by_date['2026-09-15']['calls'], 0)


class UsageSummaryTests(TestCase):
    def test_totals_match_seeded_window(self):
        events = [
            ev(
                created_at=datetime(2026, 9, 16, 1, 0, tzinfo=UTC),
                tokens_in=10,
                tokens_out=20,
                latency_ms=100,
            ),
            ev(
                created_at=datetime(2026, 9, 16, 5, 0, tzinfo=UTC),
                tokens_in=30,
                tokens_out=40,
                latency_ms=300,
            ),
            # Outside the 7-day window: must not leak into the totals.
            ev(created_at=datetime(2026, 9, 1, 5, 0, tzinfo=UTC), tokens_in=1000, latency_ms=5),
        ]
        summary = usage_summary(events, days=7, now=NOW)
        self.assertEqual(summary['total_calls'], 2)
        self.assertEqual(summary['tokens_in'], 40)
        self.assertEqual(summary['tokens_out'], 60)
        self.assertEqual(summary['avg_latency_ms'], 200)
        self.assertEqual(summary['p95_latency_ms'], 300)

    def test_empty_window_is_all_zeros(self):
        summary = usage_summary([], days=7, now=NOW)
        self.assertEqual(summary['total_calls'], 0)
        self.assertEqual(summary['tokens_in'], 0)
        self.assertEqual(summary['p95_latency_ms'], 0)


class ChartBarsTests(TestCase):
    def test_bar_heights_scale_with_calls(self):
        daily = [
            {'calls': 1, 'date': datetime(2026, 9, 16, tzinfo=UTC).date()},
            {'calls': 5, 'date': datetime(2026, 9, 17, tzinfo=UTC).date()},
        ]
        chart = chart_bars(daily)
        self.assertEqual(chart['max_calls'], 5)
        self.assertGreater(chart['bars'][1]['h'], chart['bars'][0]['h'])

    def test_zero_calls_render_a_flat_baseline(self):
        daily = [{'calls': 0, 'date': datetime(2026, 9, 17, tzinfo=UTC).date()}]
        chart = chart_bars(daily)
        self.assertEqual(chart['max_calls'], 1)
        self.assertEqual(chart['bars'][0]['h'], 0)
