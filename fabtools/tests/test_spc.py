"""SPC control-chart checker tests.

Every fixture below was verified this session against the real analyzer:
each fires exactly its target Nelson rule and no other rule. The baseline
(`BENIGN`) alternates around mean ~100 with jitter pairs whose values sit
between 1σ and 2σ; a repeated value after each jitter pair breaks strict
alternation so rule 4 stays quiet on an in-control series.

Fixture numbers are hand-derived and pinned exactly (or to tight float
tolerance) — the same standard as the wafer-map golden tests.
"""

import json

import pytest

from fabtools import spc

# 15 pairs; jitter pairs at 3, 6, 10; every pair sums to 200. A repeat of the
# pair's second value follows jitter pairs, so no long alternating run builds.
_BENIGN_PAIRS = [
    (99, 101), (99, 101), (97.5, 102.5), (99, 101), (99, 101),
    (97.5, 102.5), (99, 101), (99, 101), (99, 101), (97.5, 102.5),
    (99, 101), (99, 101), (99, 101), (99, 101), (99, 101),
]
BENIGN: list[float] = []
for _idx, _pair in enumerate(_BENIGN_PAIRS, start=1):
    BENIGN.extend(_pair)
    if _idx in (3, 6, 10):
        BENIGN.append(_pair[1])

SIGMA = 2.0


def nelson_rules(block):
    return [violation['rule'] for violation in block['violations']]


class TestParseSeries:
    def test_csv_string_parses(self):
        assert spc.parse_series('12, 13.5, 14') == [12.0, 13.5, 14.0]

    def test_semicolons_and_newlines_parse(self):
        assert spc.parse_series('1; 2\n3\t4') == [1.0, 2.0, 3.0, 4.0]

    def test_json_array_parses(self):
        assert spc.parse_series('[1, 2, 3]') == [1.0, 2.0, 3.0]

    def test_list_passes_through_validated(self):
        assert spc.parse_series([1, '2', 3.5]) == [1.0, 2.0, 3.5]

    def test_rejects_empty(self):
        with pytest.raises(spc.SpcInputError):
            spc.parse_series('   ')

    def test_rejects_non_numeric_text(self):
        with pytest.raises(spc.SpcInputError, match='not a number'):
            spc.parse_series('1, 2, oops')

    def test_rejects_json_scalar(self):
        # a scalar parses as a 1-point series, which the length check rejects
        with pytest.raises(spc.SpcInputError, match='at least 2 points'):
            spc.parse_series('42')

    def test_rejects_wrong_type_element(self):
        with pytest.raises(spc.SpcInputError, match='booleans are not measurements'):
            spc.parse_series([1, True])

    def test_rejects_non_finite_float(self):
        with pytest.raises(spc.SpcInputError, match='not a finite measurement'):
            spc.parse_series([1.0, float('nan')])

    def test_rejects_over_1000_points(self):
        with pytest.raises(spc.SpcInputError, match='at most 1000'):
            spc.parse_series([1.0] * 1001)


class TestControlLimits:
    def test_known_sigma_limits(self):
        # 8 points alternating around 100.25; sum = 802, all |z| < 1.2 with sigma 2
        block = spc.analyze_series([100, 102, 98, 102, 100, 98, 100, 102], sigma=2.0)
        assert block['mean'] == pytest.approx(100.25, abs=1e-9)
        assert block['sigma'] == pytest.approx(2.0, abs=1e-9)
        assert block['ucl'] == pytest.approx(106.25, abs=1e-9)
        assert block['lcl'] == pytest.approx(94.25, abs=1e-9)
        assert block['sigma_source'] == 'provided'

    def test_estimated_sigma_from_moving_range(self):
        # MR-bar = mean of the 9 nonzero moving ranges = 10/9; sigma = MRbar/1.128.
        # The payload rounds stats to the module's precision (_ROUND decimals).
        block = spc.analyze_series([10, 11, 10, 11, 10, 11, 10, 11, 10, 12])
        expected_sigma = (10 / 9) / 1.128
        assert block['sigma_source'] == 'estimated_from_moving_range'
        assert block['mean'] == pytest.approx(10.6, abs=1e-9)
        assert block['sigma'] == pytest.approx(round(expected_sigma, spc._ROUND), abs=1e-9)
        assert block['ucl'] == pytest.approx(round(10.6 + 3 * expected_sigma, spc._ROUND), abs=1e-9)
        assert block['lcl'] == pytest.approx(round(10.6 - 3 * expected_sigma, spc._ROUND), abs=1e-9)

    def test_zero_sigma_is_an_input_error(self):
        with pytest.raises(spc.SpcInputError, match='positive, finite number'):
            spc.analyze_series([1.0, 2.0], sigma=0)

    def test_degenerate_series_reports_indeterminate(self):
        block = spc.analyze_series([100.0] * 10)
        assert block['verdict'] == 'indeterminate'
        assert block['sigma'] == pytest.approx(0.0, abs=1e-12)
        assert block['violations'] == []
        assert any('no variation' in issue for issue in block['issues'])


class TestNelsonRules:
    """Each fixture fires exactly its target rule (verified against the analyzer)."""

    def test_clean_baseline_fires_nothing(self):
        block = spc.analyze_series(BENIGN, sigma=SIGMA)
        assert block['verdict'] == 'in_control'
        assert block['violations'] == []
        assert block['issues'] == []
        # payload rounds stats to the module's precision (_ROUND decimals)
        expected_mean = round(3307.5 / 33, spc._ROUND)
        assert block['mean'] == pytest.approx(expected_mean, abs=1e-9)
        assert block['ucl'] == pytest.approx(round(expected_mean + 6.0, spc._ROUND), abs=1e-9)
        assert block['lcl'] == pytest.approx(round(expected_mean - 6.0, spc._ROUND), abs=1e-9)

    def test_rule1_beyond_three_sigma(self):
        block = spc.analyze_series(BENIGN[:28] + [112.0, 100.0], sigma=SIGMA)
        assert nelson_rules(block) == ['nelson_1']
        assert block['violations'][0]['points'] == [29]
        assert block['violations'][0]['label'].startswith('Nelson rule 1')
        assert 'beyond 3σ' in block['violations'][0]['detail']

    def test_rule1_boundary_not_beyond(self):
        # 106 lands ~2.8σ above the baseline's mean: past 2σ (rule 5 territory),
        # never past 3σ, and the alternating baseline keeps run rules quiet.
        block = spc.analyze_series(BENIGN[:29] + [106.0], sigma=SIGMA)
        assert block['violations'] == []

    def test_rule2_nine_above_center_line(self):
        block = spc.analyze_series(BENIGN[:22] + [101.0] * 8, sigma=SIGMA)
        assert nelson_rules(block) == ['nelson_2']
        assert block['violations'][0]['points'] == list(range(22, 31))

    def test_rule3_six_increasing(self):
        block = spc.analyze_series(
            BENIGN[:24] + [100.0, 101.0, 102.0, 103.0, 104.0, 105.0], sigma=SIGMA
        )
        assert nelson_rules(block) == ['nelson_3']
        assert block['violations'][0]['points'] == list(range(24, 31))
        assert 'steadily increasing' in block['violations'][0]['detail']

    def test_rule4_alternating(self):
        # diffs strictly alternate while points straddle 1σ on both sides,
        # keeping rules 7 and 8 quiet
        block = spc.analyze_series([99.0, 101.0, 97.5, 102.5] * 4, sigma=SIGMA)
        assert nelson_rules(block) == ['nelson_4']
        assert block['violations'][0]['points'] == list(range(1, 17))
        assert block['mean'] == pytest.approx(100.0, abs=1e-9)

    def test_rule5_two_of_three_beyond_two_sigma(self):
        block = spc.analyze_series(BENIGN[:24] + [105.5, 100.0, 105.5], sigma=SIGMA)
        assert nelson_rules(block) == ['nelson_5']
        assert block['violations'][0]['points'] == [25, 27]
        assert 'beyond 2σ' in block['violations'][0]['detail']

    def test_rule6_four_of_five_beyond_one_sigma(self):
        block = spc.analyze_series(
            BENIGN[:24] + [103.5, 104.0, 103.5, 104.0, 100.0], sigma=SIGMA
        )
        assert nelson_rules(block) == ['nelson_6']
        assert block['violations'][0]['points'] == [25, 26, 27, 28]

    def test_rule7_stratification(self):
        block = spc.analyze_series([99.0, 101.0] * 6 + [99.0, 99.0, 101.0, 99.0], sigma=SIGMA)
        assert nelson_rules(block) == ['nelson_7']
        assert block['violations'][0]['points'] == list(range(1, 17))
        assert 'within 1σ' in block['violations'][0]['detail']

    def test_rule8_mixture(self):
        block = spc.analyze_series(BENIGN[:21] + [97.5, 103.0] * 4, sigma=SIGMA)
        assert nelson_rules(block) == ['nelson_8']
        assert block['violations'][0]['points'] == list(range(21, 30))

    def test_short_series_reports_unevaluated_rules(self):
        block = spc.analyze_series([10, 11, 10, 11, 10, 11, 10, 11, 10, 12])
        assert block['verdict'] == 'in_control'
        assert block['violations'] == []
        assert any('rule 4 not evaluated' in issue for issue in block['issues'])
        assert any('rule 7 not evaluated' in issue for issue in block['issues'])


class TestBlockContract:
    def test_payload_shape_and_json_round_trip(self):
        block = spc.analyze_series(BENIGN[:28] + [112.0, 100.0], sigma=SIGMA)
        assert block['type'] == 'spc_chart'
        assert block['title'] == 'SPC control-chart check'
        for key in (
            'summary', 'verdict', 'series_length', 'sigma_source', 'mean',
            'sigma', 'ucl', 'lcl', 'values', 'point_flags', 'violations',
            'issues', 'references',
        ):
            assert key in block
        assert block['verdict'] == 'out_of_control'
        assert block['series_length'] == 30
        # every value survives a JSON round trip (SSE payload contract)
        assert json.loads(json.dumps(block)) == block
        # point_flags runs parallel to values (0-indexed); violations are 1-based
        assert block['point_flags'][28] == ['nelson_1']
        assert block['point_flags'][0] == []
        assert block['values'][28] == 112.0
        assert len(block['point_flags']) == 30

    def test_point_flags_name_rules(self):
        block = spc.analyze_series(
            BENIGN[:24] + [103.5, 104.0, 103.5, 104.0, 100.0], sigma=SIGMA
        )
        flagged = {
            index + 1: rules for index, rules in enumerate(block['point_flags']) if rules
        }
        assert flagged == {25: ['nelson_6'], 26: ['nelson_6'], 27: ['nelson_6'], 28: ['nelson_6']}

    def test_references_are_strings(self):
        block = spc.analyze_series(BENIGN, sigma=SIGMA)
        assert block['references'] == [
            'Nelson, L.S. (1984). "The Shewhart Control Chart — Tests for '
            'Special Causes". Journal of Quality Technology 16(4), pp. 237-239.',
            'Western Electric Company (1956). Statistical Quality Control '
            'Handbook, Section 3 (zone tests for lack of control).',
        ]
        assert all(isinstance(ref, str) for ref in block['references'])


class TestCheckSeriesBoundary:
    """The tool-boundary function never raises; failures come back as blocks."""

    def test_error_block_for_bad_input(self):
        block = spc.check_series('1, 2, oops', None)
        assert block['type'] == 'error'
        assert 'not a number' in block['detail']
        assert block['code'] == 'spc_input_invalid'
        assert block['issues'] == []

    def test_error_block_for_number_series(self):
        block = spc.check_series(123, None)
        assert block['type'] == 'error'
        assert 'series' in block['detail']

    def test_error_block_for_zero_sigma(self):
        block = spc.check_series('1, 2, 3', 0)
        assert block['type'] == 'error'
        assert 'positive, finite number' in block['detail']

    def test_valid_call_matches_analyzer(self):
        series = BENIGN[:28] + [112.0, 100.0]
        assert spc.check_series(series, str(SIGMA)) == spc.analyze_series(series, sigma=SIGMA)

    def test_unexpected_exception_returns_structured_error(self, monkeypatch):
        def boom(_series):
            raise RuntimeError('boom')

        monkeypatch.setattr(spc, '_parse_series_text', boom)
        block = spc.check_series('anything', None)
        assert block['type'] == 'error'
        assert block['detail'] == 'unexpected failure while checking the series: RuntimeError: boom'
        assert block['code'] == 'spc_analysis_failed'

    def test_truncated_series_hint(self):
        block = spc.check_series(json.dumps([1.0] * 1001), None)
        assert block['type'] == 'error'
        assert 'at most 1000' in block['detail']
        assert 'split it into overlapping windows' in block['detail']
