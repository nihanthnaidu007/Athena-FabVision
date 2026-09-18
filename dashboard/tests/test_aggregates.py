"""Unit tests for the pure dashboard aggregation helpers (no database)."""

from datetime import datetime
from datetime import timezone as dt_timezone
from types import SimpleNamespace

from django.test import TestCase

from dashboard.aggregates import (
    breakdowns,
    chart_bars,
    daily_series,
    feedback_summary,
    p95,
    parse_days,
    usage_summary,
)

UTC = dt_timezone.utc
NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)


def ev(
    *,
    created_at,
    tokens_in=0,
    tokens_out=0,
    latency_ms=0,
    kind='chat',
    model='',
    tool_name='',
):
    """A UsageEvent-shaped stand-in; aggregates only need these attributes."""
    return SimpleNamespace(
        created_at=created_at,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        latency_ms=latency_ms,
        kind=kind,
        model=model,
        tool_name=tool_name,
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


class BreakdownsTests(TestCase):
    def test_groups_by_kind_model_and_tool(self):
        events = [
            ev(created_at=NOW, tokens_in=10, tokens_out=20, kind='chat', model='gpt-4o-mini'),
            ev(created_at=NOW, tokens_in=1, tokens_out=2, kind='api', model='gpt-4o-mini'),
            ev(
                created_at=NOW,
                tokens_in=5,
                tokens_out=5,
                kind='tool',
                tool_name='wafer_map_analyze',
            ),
        ]
        result = breakdowns(events, days=7, now=NOW)
        # Ties break alphabetically, so the equal-call kinds sort by label.
        self.assertEqual([row['label'] for row in result['kind']], ['api', 'chat', 'tool'])
        self.assertEqual(result['model'][0]['label'], 'gpt-4o-mini')
        self.assertEqual(result['model'][0]['calls'], 2)
        self.assertEqual(result['model'][0]['tokens_in'], 11)
        self.assertEqual(result['model'][0]['tokens_out'], 22)
        # 'unspecified' (2 events) outranks the one stamped tool call.
        self.assertEqual(
            [row['label'] for row in result['tool']], ['unspecified', 'wafer_map_analyze']
        )
        self.assertEqual(result['tool'][1]['calls'], 1)

    def test_blank_fields_group_under_unspecified(self):
        events = [ev(created_at=NOW, kind='chat')]  # no model, no tool
        result = breakdowns(events, days=7, now=NOW)
        self.assertEqual([row['label'] for row in result['model']], ['unspecified'])
        self.assertEqual([row['label'] for row in result['tool']], ['unspecified'])
        self.assertEqual(result['model'][0]['calls'], 1)

    def test_events_outside_window_are_ignored(self):
        events = [
            ev(created_at=NOW, kind='chat', tokens_in=10),
            ev(created_at=datetime(2026, 9, 1, tzinfo=UTC), kind='voice', tokens_in=999),
        ]
        result = breakdowns(events, days=7, now=NOW)
        self.assertEqual([row['label'] for row in result['kind']], ['chat'])
        self.assertEqual(result['kind'][0]['tokens_in'], 10)

    def test_rows_sort_by_calls_descending(self):
        events = [ev(created_at=NOW, kind='api')] + [ev(created_at=NOW, kind='chat')] * 3
        result = breakdowns(events, days=7, now=NOW)
        self.assertEqual([row['label'] for row in result['kind']], ['chat', 'api'])
        self.assertEqual(result['kind'][0]['calls'], 3)

    def test_empty_window_gives_empty_breakdowns(self):
        result = breakdowns([], days=7, now=NOW)
        self.assertEqual(result, {'kind': [], 'model': [], 'tool': []})


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


def fb(*, value, conversation_title='Conv', tools=()):
    """A MessageFeedback-shaped stand-in: value + message with conversation/sources."""
    return SimpleNamespace(
        value=value,
        message=SimpleNamespace(
            conversation=SimpleNamespace(title=conversation_title),
            sources=[{'kind': 'tool', 'tool': tool} for tool in tools],
        ),
    )


class FeedbackSummaryTests(TestCase):
    """feedback_summary: totals, ratios, and tool attribution (no database)."""

    def test_empty_feedback_has_zero_totals_and_no_rows(self):
        self.assertEqual(
            feedback_summary([]),
            {
                'total': 0,
                'ups': 0,
                'downs': 0,
                'up_ratio': None,
                'conversations': [],
                'tools': [],
            },
        )

    def test_totals_and_overall_ratio(self):
        summary = feedback_summary([fb(value='up'), fb(value='up'), fb(value='down')])
        self.assertEqual(summary['total'], 3)
        self.assertEqual(summary['ups'], 2)
        self.assertEqual(summary['downs'], 1)
        self.assertEqual(summary['up_ratio'], 67)  # round(2/3 * 100)

    def test_conversation_rows_sort_by_volume_then_label(self):
        summary = feedback_summary(
            [
                fb(value='up', conversation_title='Lithography'),
                fb(value='down', conversation_title='Etch'),
                fb(value='down', conversation_title='Etch'),
            ]
        )
        rows = summary['conversations']
        self.assertEqual([row['label'] for row in rows], ['Etch', 'Lithography'])
        self.assertEqual(
            rows[0], {'label': 'Etch', 'ups': 0, 'downs': 2, 'total': 2, 'up_ratio': 0}
        )
        self.assertEqual(
            rows[1], {'label': 'Lithography', 'ups': 1, 'downs': 0, 'total': 1, 'up_ratio': 100}
        )

    def test_tool_rows_attribute_through_message_sources(self):
        summary = feedback_summary(
            [
                fb(value='up', tools=['wafer_map']),
                fb(value='down', tools=['wafer_map', 'kb_search']),
            ]
        )
        tools = {row['label']: row for row in summary['tools']}
        self.assertEqual(tools['wafer_map']['ups'], 1)
        self.assertEqual(tools['wafer_map']['downs'], 1)
        self.assertEqual(tools['kb_search']['downs'], 1)
        # The conversation row still counts each answer exactly once.
        self.assertEqual(summary['conversations'][0]['total'], 2)

    def test_feedback_without_tool_sources_feeds_only_conversations(self):
        summary = feedback_summary([fb(value='up', tools=[])])
        self.assertEqual(summary['tools'], [])
        self.assertEqual(summary['conversations'][0]['ups'], 1)
