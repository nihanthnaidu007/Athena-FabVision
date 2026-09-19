"""8D report assembly: prompt building, JSON recovery, schema validation.

Pure module (spec #11) -- no Django, no network, no wall clock. These
tests pin the honest-degradation contract: every malformed model reply
raises :class:`fabtools.rca_report.ReportValidationError` naming the
field, and the transcript bound announces its truncation.
"""

from __future__ import annotations

import pytest

from fabtools import rca_report

# A minimal valid payload: required fields only.
VALID_PAYLOAD = {
    'title': 'Lot 42A edge-ring excursion',
    'problem': 'Edge-ring yield loss after the etch chamber PM.',
    'timeline': [{'when': '2026-09-14', 'event': 'PM completed; first post-PM lot started.'}],
    'containment': ['Held lot 42A.'],
    'root_cause': 'PM left a drifted gap setting.',
    'corrective_actions': [{'action': 'Recalibrate the gap setting.', 'owner': 'Etch owner'}],
}


class TestExtractJsonPayload:
    def test_plain_object_parses(self):
        assert rca_report.extract_json_payload('{"title": "t"}') == {'title': 't'}

    def test_fenced_json_parses_even_with_prose_around(self):
        reply = 'Here is the report:\n```json\n{"title": "t"}\n```\nDone.'
        assert rca_report.extract_json_payload(reply) == {'title': 't'}

    def test_object_sandwiched_in_prose_parses(self):
        reply = 'Sure. {"title": "t", "timeline": [{"when": "d", "event": "e"}]} Hope it helps.'
        payload = rca_report.extract_json_payload(reply)
        assert payload['title'] == 't'

    def test_nested_braces_survive_the_fence(self):
        reply = '```json\n{"a": {"b": 1}, "c": [{}]}\n```'
        assert rca_report.extract_json_payload(reply) == {'a': {'b': 1}, 'c': [{}]}

    def test_no_json_raises_naming_the_problem(self):
        with pytest.raises(rca_report.ReportValidationError, match='no JSON object'):
            rca_report.extract_json_payload('I cannot assemble a report.')

    def test_empty_reply_raises(self):
        with pytest.raises(rca_report.ReportValidationError, match='empty'):
            rca_report.extract_json_payload('   ')

    def test_broken_json_raises(self):
        with pytest.raises(rca_report.ReportValidationError, match='not valid JSON'):
            rca_report.extract_json_payload('{"title": }')


class TestValidateReport:
    def test_minimal_valid_payload_normalizes(self):
        report = rca_report.validate_report(VALID_PAYLOAD)
        assert report['title'] == 'Lot 42A edge-ring excursion'
        assert report['containment'] == ['Held lot 42A.']
        assert report['timeline'] == [
            {'when': '2026-09-14', 'event': 'PM completed; first post-PM lot started.'}
        ]
        assert report['corrective_actions'] == [
            {'action': 'Recalibrate the gap setting.', 'owner': 'Etch owner', 'due': ''}
        ]
        # Optional sections default to empty, never to None.
        assert report['team'] == []
        assert report['root_cause_candidates'] == []
        assert report['verification'] == []
        assert report['prevention'] == []
        assert report['closure'] == ''

    def test_unknown_extra_keys_are_ignored(self):
        payload = {**VALID_PAYLOAD, 'mood': 'apologetic'}
        assert rca_report.validate_report(payload)['title'] == VALID_PAYLOAD['title']

    def test_scalar_fields_are_stripped(self):
        payload = {**VALID_PAYLOAD, 'title': '  padded title  '}
        assert rca_report.validate_report(payload)['title'] == 'padded title'

    @pytest.mark.parametrize('field', ['title', 'problem', 'root_cause'])
    def test_missing_required_text_names_the_field(self, field):
        payload = {key: value for key, value in VALID_PAYLOAD.items() if key != field}
        with pytest.raises(rca_report.ReportValidationError, match=f"'{field}'"):
            rca_report.validate_report(payload)

    @pytest.mark.parametrize('field', ['timeline', 'containment', 'corrective_actions'])
    def test_missing_required_list_names_the_field(self, field):
        payload = {key: value for key, value in VALID_PAYLOAD.items() if key != field}
        with pytest.raises(rca_report.ReportValidationError, match=f"'{field}'"):
            rca_report.validate_report(payload)

    @pytest.mark.parametrize('field', ['title', 'problem', 'root_cause'])
    def test_blank_required_text_is_an_error(self, field):
        payload = {**VALID_PAYLOAD, field: '   '}
        with pytest.raises(rca_report.ReportValidationError, match=f"'{field}'"):
            rca_report.validate_report(payload)

    def test_empty_required_list_is_an_error(self):
        payload = {**VALID_PAYLOAD, 'containment': []}
        with pytest.raises(rca_report.ReportValidationError, match="'containment'"):
            rca_report.validate_report(payload)

    def test_timeline_entry_missing_event_is_named(self):
        payload = {**VALID_PAYLOAD, 'timeline': [{'when': '2026-09-14'}]}
        with pytest.raises(rca_report.ReportValidationError, match='timeline entry 1'):
            rca_report.validate_report(payload)

    def test_timeline_non_object_entry_is_named(self):
        payload = {**VALID_PAYLOAD, 'timeline': ['PM completed']}
        with pytest.raises(rca_report.ReportValidationError, match="timeline' entry 1"):
            rca_report.validate_report(payload)

    def test_action_missing_is_named(self):
        payload = {**VALID_PAYLOAD, 'corrective_actions': [{'owner': 'Etch owner'}]}
        with pytest.raises(rca_report.ReportValidationError, match='corrective_actions entry 1'):
            rca_report.validate_report(payload)

    def test_list_field_with_object_entries_is_named(self):
        payload = {**VALID_PAYLOAD, 'team': [{'name': 'Etch owner'}]}
        with pytest.raises(rca_report.ReportValidationError, match="'team'"):
            rca_report.validate_report(payload)

    def test_empty_string_list_entries_are_named_not_dropped(self):
        payload = {**VALID_PAYLOAD, 'containment': ['Held lot 42A.', '   ']}
        with pytest.raises(rca_report.ReportValidationError, match='entry 2'):
            rca_report.validate_report(payload)

    def test_non_object_payload_is_rejected(self):
        with pytest.raises(rca_report.ReportValidationError, match='JSON object'):
            rca_report.validate_report(['not', 'a', 'dict'])


class TestBuildReportMessages:
    def test_two_messages_with_system_first(self):
        messages = rca_report.build_report_messages('A title', 'transcript body')
        assert [message['role'] for message in messages] == ['system', 'user']
        assert '8D' in messages[0]['content']
        assert 'never invent facts' in messages[0]['content']

    def test_user_message_carries_title_and_transcript(self):
        messages = rca_report.build_report_messages('A title', 'the transcript')
        assert 'Conversation title: A title' in messages[1]['content']
        assert 'the transcript' in messages[1]['content']
        assert messages[1]['content'].endswith('Return the 8D report JSON object now.')

    def test_short_transcript_passes_through_untouched(self):
        messages = rca_report.build_report_messages('t', 'short transcript')
        assert 'short transcript' in messages[1]['content']
        assert 'omitted' not in messages[1]['content']

    def test_long_transcript_keeps_head_and_tail_with_a_marker(self):
        transcript = 'HEAD' + 'x' * 40_000 + 'TAIL'
        messages = rca_report.build_report_messages('t', transcript)
        content = messages[1]['content']
        assert 'HEAD' in content and 'TAIL' in content
        assert 'middle of the transcript omitted' in content
        assert len(content) < len(transcript)

    def test_bounded_transcript_is_exact_under_the_cap(self):
        transcript = 'x' * rca_report.MAX_TRANSCRIPT_CHARS
        assert rca_report.bounded_transcript(transcript) is transcript
