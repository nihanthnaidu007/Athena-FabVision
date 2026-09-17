"""Excursion triage rule tests: thresholds, severities, reasons, block shape."""

from fabtools import excursion


class TestTriageRules:
    """Threshold-based flagging with reasons (each row: rule, severity, ...)."""

    def test_critical_yield_flags_critical_rule(self):
        block = excursion.triage_lot({'yield_pct': 62.5})
        assert block['type'] == 'table'
        assert block['flags'] == ['YIELD_CRITICAL']
        assert block['rows'][0][1] == 'critical'
        assert '62.5%' in block['rows'][0][2]

    def test_watch_band_yield_flags_warning(self):
        block = excursion.triage_lot({'yield_pct': 90.0})
        assert block['flags'] == ['YIELD_WATCH']
        assert block['rows'][0][1] == 'warning'

    def test_healthy_yield_flags_nothing(self):
        block = excursion.triage_lot({'yield_pct': 98.0})
        assert block['flags'] == []
        assert block['rows'] == []
        assert 'within thresholds' in block['summary']

    def test_threshold_boundaries_are_exact(self):
        at_critical = excursion.triage_lot({'yield_pct': excursion.YIELD_CRITICAL_BELOW})
        assert at_critical['flags'] == ['YIELD_WATCH']  # strictly below 80 is critical
        at_watch = excursion.triage_lot({'yield_pct': excursion.YIELD_WATCH_BELOW})
        assert at_watch['flags'] == []  # strictly below 95 is a watch

    def test_defect_density_severity_bands(self):
        warning = excursion.triage_lot({'yield_pct': 99, 'defect_density': 0.7})
        assert warning['flags'] == ['DEFECT_DENSITY_HIGH']
        critical = excursion.triage_lot({'yield_pct': 99, 'defect_density': 1.5})
        assert critical['flags'] == ['DEFECT_DENSITY_CRITICAL']
        assert critical['rows'][0][1] == 'critical'

    def test_lot_spread_rule_flags_lot_level_excursion(self):
        block = excursion.triage_lot({'yield_pct': 99, 'wafers_flagged_pct': 45})
        assert block['flags'] == ['LOT_SPREAD']
        assert block['rows'][0][4]  # carries a human-readable reason

    def test_multiple_flags_are_all_reported_in_order(self):
        block = excursion.triage_lot({'yield_pct': 60, 'defect_density': 2.0,
                                      'wafers_flagged_pct': 50})
        assert block['flags'] == ['YIELD_CRITICAL', 'DEFECT_DENSITY_CRITICAL', 'LOT_SPREAD']

    def test_unknown_metrics_are_ignored(self):
        block = excursion.triage_lot({'yield_pct': 99, 'flavor': 'banana'})
        assert block['flags'] == []
        assert block['type'] == 'table'

    def test_critical_flags_drive_quarantine_checklist(self):
        block = excursion.triage_lot({'yield_pct': 60})
        assert any('Quarantine' in step for step in block['checklist'])

    def test_clean_lot_gets_monitor_checklist(self):
        block = excursion.triage_lot({'yield_pct': 99})
        assert any('Monitor' in step for step in block['checklist'])


class TestTriageValidation:
    """Invalid metrics return the structured error block, never raise."""

    def test_missing_yield_is_error_block(self):
        block = excursion.triage_lot({'defect_density': 0.1})
        assert block['type'] == 'error'
        assert 'yield_pct' in block['detail']

    def test_non_numeric_yield_is_error_block(self):
        block = excursion.triage_lot({'yield_pct': 'high'})
        assert block['type'] == 'error'
        assert "'yield_pct'" in block['detail']

    def test_out_of_range_yield_is_error_block(self):
        block = excursion.triage_lot({'yield_pct': 150})
        assert block['type'] == 'error'
        assert 'between 0 and 100' in block['detail']

    def test_negative_density_is_error_block(self):
        block = excursion.triage_lot({'yield_pct': 99, 'defect_density': -1})
        assert block['type'] == 'error'
        assert 'non-negative' in block['detail']

    def test_error_block_carries_contract_keys(self):
        block = excursion.triage_lot({})
        assert block['type'] == 'error'
        assert 'detail' in block and 'code' in block and 'title' in block

    def test_table_rows_match_column_count(self):
        block = excursion.triage_lot({'yield_pct': 70, 'defect_density': 0.9})
        assert block['columns'] == ['Rule', 'Severity', 'Observed', 'Threshold', 'Reason']
        assert all(len(row) == len(block['columns']) for row in block['rows'])
