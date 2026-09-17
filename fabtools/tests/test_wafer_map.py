"""Golden wafer-map analyzer tests.

The fixtures under ``fixtures/`` are generated deterministically: an 81-die
circular wafer (x^2 + y^2 <= 25 on a +/-5 grid). Generator math verified
this session: 81 dies, outer band (distance >= 4.5) holds 12, center region
(distance <= 1.25) holds 5.

- clean_wafer.csv     all 81 bin 1    -> no patterns
- edge_ring.csv       outer 12 bin 2  -> edge_ring, yield 69/81
- center_hotspot.csv  center 5 bin 2  -> center_hotspot, yield 76/81
- malformed_*.csv     schema faults   -> structured error blocks

Numbers are asserted exactly (or to tight float tolerance) — spec criterion
5 demands exact yield/bin/edge-ring numbers, not vibes.
"""

from pathlib import Path

import pytest

from fabtools import wafer_map

FIXTURES = Path(__file__).parent / 'fixtures'


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding='utf-8')


class TestGoldenFixtures:
    """Golden CSVs produce exact numbers (spec criterion 5)."""

    def test_clean_wafer_golden_numbers(self):
        block = wafer_map.analyze_wafer_csv(load_fixture('clean_wafer.csv'))
        assert block['type'] == 'wafer_map'
        assert block['total_dies'] == 81
        assert block['pass_count'] == 81
        assert block['fail_count'] == 0
        assert block['yield_pct'] == pytest.approx(1.0, abs=1e-9)
        assert block['bin_counts'] == {'1': 81}
        assert block['patterns'] == []
        assert block['edge_ring_score'] == pytest.approx(0.0, abs=1e-9)
        assert block['center_hotspot_score'] == pytest.approx(0.0, abs=1e-9)
        assert block['issues'] == []

    def test_edge_ring_fixture_flags_edge_ring(self):
        block = wafer_map.analyze_wafer_csv(load_fixture('edge_ring.csv'))
        assert block['type'] == 'wafer_map'
        assert block['total_dies'] == 81
        assert block['pass_count'] == 69
        assert block['fail_count'] == 12
        assert block['yield_pct'] == pytest.approx(69 / 81, abs=1e-6)
        assert block['bin_counts'] == {'1': 69, '2': 12}
        assert block['patterns'] == ['edge_ring']
        # outer band fails 100%, wafer mean is 12/81
        assert block['edge_ring_score'] == pytest.approx(1 - 12 / 81, abs=1e-4)
        # the 5-die center region is clean -> below-baseline, not flagged
        assert block['center_hotspot_score'] == pytest.approx(0 - 12 / 76, abs=1e-4)
        assert 'center_hotspot' not in block['patterns']

    def test_center_hotspot_fixture_flags_hotspot(self):
        block = wafer_map.analyze_wafer_csv(load_fixture('center_hotspot.csv'))
        assert block['type'] == 'wafer_map'
        assert block['total_dies'] == 81
        assert block['pass_count'] == 76
        assert block['yield_pct'] == pytest.approx(76 / 81, abs=1e-6)
        assert block['bin_counts'] == {'1': 76, '2': 5}
        assert block['patterns'] == ['center_hotspot']
        assert block['center_hotspot_score'] == pytest.approx(1.0, abs=1e-4)
        assert block['edge_ring_score'] == pytest.approx(0 - 5 / 81, abs=1e-4)
        assert 'edge_ring' not in block['patterns']

    def test_patterns_list_is_empty_on_clean_map(self):
        block = wafer_map.analyze_wafer_csv(load_fixture('clean_wafer.csv'))
        assert block['patterns'] == []
        assert 'none' in block['summary']

    def test_summary_matches_counts(self):
        block = wafer_map.analyze_wafer_csv(load_fixture('edge_ring.csv'))
        assert '69/81 die pass' in block['summary']
        assert 'edge_ring' in block['summary']


class TestStrictValidation:
    """Malformed input returns the structured error block, never raises."""

    def test_missing_column_fixture_is_structured_error(self):
        block = wafer_map.analyze_wafer_csv(load_fixture('malformed_missing_column.csv'))
        assert block['type'] == 'error'
        assert 'missing required column' in block['detail']
        assert block['code'] == 'wafer_csv_invalid'

    def test_non_integer_bin_is_structured_error(self):
        block = wafer_map.analyze_wafer_csv(load_fixture('malformed_bad_bin.csv'))
        assert block['type'] == 'error'
        assert "'bin'" in block['detail']
        assert block['code'] == 'wafer_csv_invalid'

    def test_error_blocks_carry_contract_keys(self):
        for name in ('malformed_missing_column.csv', 'malformed_bad_bin.csv'):
            block = wafer_map.analyze_wafer_csv(load_fixture(name))
            assert block['type'] == 'error'
            assert 'detail' in block and 'code' in block and 'title' in block

    @pytest.mark.parametrize('bad', ['', '   ', '\n\n'])
    def test_empty_csv_is_structured_error(self, bad):
        block = wafer_map.analyze_wafer_csv(bad)
        assert block['type'] == 'error'
        assert 'empty' in block['detail']

    def test_header_only_csv_is_structured_error(self):
        block = wafer_map.analyze_wafer_csv('wafer_id,x,y,bin\n')
        assert block['type'] == 'error'
        assert 'no data rows' in block['detail']

    def test_empty_wafer_id_is_structured_error(self):
        block = wafer_map.analyze_wafer_csv('wafer_id,x,y,bin\n,0,0,1\n')
        assert block['type'] == 'error'
        assert "'wafer_id'" in block['detail']

    def test_negative_bin_is_structured_error(self):
        block = wafer_map.analyze_wafer_csv('wafer_id,x,y,bin\nW1,0,0,-3\n')
        assert block['type'] == 'error'
        assert 'non-negative' in block['detail']

    def test_ragged_row_is_structured_error_with_row_number(self):
        block = wafer_map.analyze_wafer_csv('wafer_id,x,y,bin\nW1,0,0,1\nW1,1,1\n')
        assert block['type'] == 'error'
        assert 'row 3' in block['detail']

    def test_duplicate_columns_are_structured_error(self):
        block = wafer_map.analyze_wafer_csv('wafer_id,x,y,bin,bin\nW1,0,0,1,1\n')
        assert block['type'] == 'error'
        assert 'duplicate column' in block['detail']

    def test_float_coordinates_rejected(self):
        block = wafer_map.analyze_wafer_csv('wafer_id,x,y,bin\nW1,0.5,0,1\n')
        assert block['type'] == 'error'
        assert "'x'" in block['detail']


class TestBinEdgeCases:
    """Bin-code semantics: only bin 1 passes; other codes are valid fails."""

    def test_bin_zero_is_a_fail_not_an_error(self):
        block = wafer_map.analyze_wafer_csv('wafer_id,x,y,bin\nW1,0,0,0\nW1,1,0,1\n')
        assert block['type'] == 'wafer_map'
        assert block['bin_counts'] == {'0': 1, '1': 1}
        assert block['fail_count'] == 1

    def test_high_bin_codes_are_fails_and_counted(self):
        csv = 'wafer_id,x,y,bin\nW1,0,0,7\nW1,1,0,255\nW1,2,0,1\n'
        block = wafer_map.analyze_wafer_csv(csv)
        assert block['bin_counts'] == {'1': 1, '7': 1, '255': 1}
        assert block['fail_count'] == 2
        assert block['yield_pct'] == pytest.approx(1 / 3, abs=1e-6)

    def test_all_fail_map(self):
        rows = ['wafer_id,x,y,bin'] + [f'W1,{x},0,2' for x in range(6)]
        block = wafer_map.analyze_wafer_csv('\n'.join(rows) + '\n')
        assert block['yield_pct'] == pytest.approx(0.0, abs=1e-9)
        assert block['pass_count'] == 0

    def test_multiple_wafer_ids_analyzed_with_issue(self):
        csv = 'wafer_id,x,y,bin\nW1,0,0,1\nW2,1,0,1\nW1,2,0,1\n'
        block = wafer_map.analyze_wafer_csv(csv)
        assert block['type'] == 'wafer_map'
        assert any('multiple wafer_ids' in issue for issue in block['issues'])

    def test_extra_columns_are_ignored(self):
        csv = 'wafer_id,x,y,bin,lot,comment\nW1,0,0,1,A42,hello\nW1,1,0,1,A42,x\n'
        block = wafer_map.analyze_wafer_csv(csv)
        assert block['type'] == 'wafer_map'
        assert block['total_dies'] == 2

    def test_header_case_insensitive(self):
        csv = 'WAFER_ID,X,Y,BIN\nW1,0,0,1\nW1,1,0,2\n'
        block = wafer_map.analyze_wafer_csv(csv)
        assert block['type'] == 'wafer_map'
        assert block['bin_counts'] == {'1': 1, '2': 1}

    def test_small_grid_issues_reported_for_sparse_regions(self):
        # 4x1 grid: the outer band holds too few dies to evaluate honestly.
        csv = 'wafer_id,x,y,bin\n' + ''.join(f'W1,{x},0,1\n' for x in range(4))
        block = wafer_map.analyze_wafer_csv(csv)
        assert block['type'] == 'wafer_map'
        assert block['patterns'] == []
        assert any('not evaluated' in issue for issue in block['issues'])

    def test_deterministic_across_calls(self):
        content = load_fixture('edge_ring.csv')
        assert wafer_map.analyze_wafer_csv(content) == wafer_map.analyze_wafer_csv(content)
