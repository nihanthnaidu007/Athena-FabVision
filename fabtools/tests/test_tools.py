"""Tool-registry contract tests.

Validates ``fabtools.tools.TOOLS`` against the integration contract: async
callables ``(user, **kwargs)`` returning block dicts ``{type, title, ...}``
with ``type`` in {table, text, wafer_map, error}; a list of
``[name, fn, availability_fn]``; KB search delegating to
``rag.retrieval.retrieve``; web search injectable and degraded without
config. All fakes are injected — no real external API is contacted.
"""

import asyncio
import inspect

from fabtools import tools


def run(coro):
    return asyncio.run(coro)


def inject_retrieve(monkeypatch, fn):
    monkeypatch.setattr(tools, '_rag_retrieve', fn)
    monkeypatch.setattr(tools, '_rag_loaded', True)


class _FakeClient:
    def __init__(self, results=None, error=None):
        self._results = results if results is not None else [
            {'title': 'SEMATECH e-handbook', 'url': 'https://example.com/etch',
             'snippet': 'nitride etch rates'}
        ]
        self._error = error

    async def search(self, query, k=5):
        if self._error is not None:
            raise self._error
        return self._results


class TestRegistryContract:
    """The TOOLS surface follows the integration contract."""

    def test_tools_is_a_list_of_triples(self):
        assert isinstance(tools.TOOLS, list)
        for entry in tools.TOOLS:
            name, fn, availability = entry
            assert isinstance(name, str)
            assert callable(fn)
            assert callable(availability)

    def test_expected_tool_names_present(self):
        names = {name for name, _fn, _available in tools.TOOLS}
        assert {
            'wafer_map_analyze', 'excursion_triage', 'spc_rules_check',
            'kb_search', 'web_search',
        } <= names

    def test_tool_schemas_cover_the_wafer_tool(self):
        # The LLM must learn the path/csv_content arguments by name; the
        # loader merges TOOL_SCHEMAS in additively.
        schema = tools.TOOL_SCHEMAS['wafer_map_analyze']
        properties = schema['parameters']['properties']
        assert 'csv_content' in properties
        assert 'path' in properties
        schema_text = schema['description'] + properties['path']['description']
        assert 'file_path' in schema_text

    def test_tool_functions_are_async(self):
        for _name, fn, _available in tools.TOOLS:
            assert inspect.iscoroutinefunction(fn)

    def test_deterministic_tools_are_always_available(self):
        for name, _fn, available in tools.TOOLS:
            if name in ('wafer_map_analyze', 'excursion_triage', 'spc_rules_check'):
                assert available() is True

    def test_every_registered_tool_returns_a_contract_block(self):
        blocks = [
            run(tools.wafer_map_analyze(None, csv_content='wafer_id,x,y,bin\nW1,0,0,1\n')),
            run(tools.excursion_triage(None, metrics={'yield_pct': 99})),
            run(tools.spc_rules_check(None, series='100, 101, 99, 102')),
            run(tools.kb_search(None, query='etch rate')),
            run(tools.web_search(None, query='nitride etch', client=_FakeClient())),
        ]
        for block in blocks:
            assert block['type'] in {'table', 'text', 'wafer_map', 'spc_chart', 'error'}
            assert 'title' in block


class TestWaferTool:
    def test_returns_wafer_map_block_on_clean_csv(self):
        block = run(tools.wafer_map_analyze(None, csv_content='wafer_id,x,y,bin\nW1,0,0,1\n'))
        assert block['type'] == 'wafer_map'
        assert block['total_dies'] == 1

    def test_malformed_csv_is_error_block_not_exception(self):
        block = run(tools.wafer_map_analyze(None, csv_content='id,x,y\nW1,0,0\n'))
        assert block['type'] == 'error'

    def test_missing_input_is_error_block(self):
        block = run(tools.wafer_map_analyze(None))
        assert block['type'] == 'error'
        assert 'no wafer CSV provided' in block['detail']

    def test_path_input_reads_the_file(self, tmp_path):
        target = tmp_path / 'w.csv'
        target.write_text('wafer_id,x,y,bin\nW1,0,0,1\n', encoding='utf-8')
        block = run(tools.wafer_map_analyze(None, path=str(target)))
        assert block['type'] == 'wafer_map'

    def test_unreadable_path_is_error_block(self, tmp_path):
        block = run(tools.wafer_map_analyze(None, path=str(tmp_path / 'missing.csv')))
        assert block['type'] == 'error'
        assert block['code'] == 'wafer_csv_unreadable'

    def test_relative_path_without_an_owner_is_denied_not_read_from_cwd(self):
        # A relative name is a knowledge-base storage name: without a
        # resolvable owning document it is refused -- the raw filesystem
        # (CWD) is never probed with it.
        block = run(tools.wafer_map_analyze(None, path='documents/2026/09/18/w.csv'))
        assert block['type'] == 'error'
        assert block['code'] == 'wafer_csv_not_found'

    def test_traversal_shaped_relative_path_is_denied(self):
        block = run(tools.wafer_map_analyze(None, path='../../secrets.csv'))
        assert block['type'] == 'error'
        assert block['code'] == 'wafer_csv_not_found'


class TestSpcTool:
    def test_returns_spc_chart_block_on_valid_series(self):
        block = run(tools.spc_rules_check(None, series='100, 101, 99, 102'))
        assert block['type'] == 'spc_chart'
        assert block['series_length'] == 4

    def test_accepts_list_series(self):
        block = run(tools.spc_rules_check(None, series=[100.0, 101.0, 99.0, 102.0]))
        assert block['type'] == 'spc_chart'

    def test_sigma_passthrough_reaches_the_analyzer(self):
        block = run(tools.spc_rules_check(None, series='100, 101, 99, 102', sigma='2.0'))
        assert block['sigma_source'] == 'provided'

    def test_tolerates_common_argument_aliases(self):
        block = run(tools.spc_rules_check(None, values='100, 101, 99, 102'))
        assert block['type'] == 'spc_chart'

    def test_invalid_series_is_error_block_not_exception(self):
        block = run(tools.spc_rules_check(None, series='1, 2, oops'))
        assert block['type'] == 'error'
        assert block['code'] == 'spc_input_invalid'

    def test_missing_input_is_error_block(self):
        block = run(tools.spc_rules_check(None))
        assert block['type'] == 'error'
        assert 'no measurement series provided' in block['detail']


class TestKbSearchTool:
    def test_delegates_to_retrieve_and_returns_sources(self, monkeypatch):
        calls = []

        def fake_retrieve(user, query, k=5):
            calls.append((user, query, k))
            return [{
                'document_id': 7, 'chunk_id': 42, 'title': 'Diffusion spec',
                'snippet': 'ABC process windows', 'score': 0.91,
            }]

        inject_retrieve(monkeypatch, fake_retrieve)
        block = run(tools.kb_search('user-a', query='diffusion', k=2))
        assert calls == [('user-a', 'diffusion', 2)]
        assert block['type'] == 'table'
        assert block['sources'] == [{
            'document_id': 7, 'chunk_id': 42, 'title': 'Diffusion spec',
            'snippet': 'ABC process windows', 'score': 0.91,
        }]
        assert block['rows'] == [['Diffusion spec', 'ABC process windows', 0.91]]

    def test_supports_async_retrieve_implementations(self, monkeypatch):
        async def fake_retrieve(user, query, k=5):
            return [{'document_id': 1, 'chunk_id': 2, 'title': 't', 'snippet': 's', 'score': 0.5}]

        inject_retrieve(monkeypatch, fake_retrieve)
        block = run(tools.kb_search(None, query='q'))
        assert block['sources'][0]['chunk_id'] == 2

    def test_unavailable_when_rag_missing(self, monkeypatch):
        inject_retrieve(monkeypatch, None)
        assert tools.kb_search_available() is False
        block = run(tools.kb_search(None, query='q'))
        assert block['type'] == 'error'
        assert block['code'] == 'kb_search_unavailable'

    def test_retrieve_failure_is_error_block_never_raises(self, monkeypatch):
        def boom(user, query, k=5):
            raise RuntimeError('rag exploded')

        inject_retrieve(monkeypatch, boom)
        block = run(tools.kb_search(None, query='q'))
        assert block['type'] == 'error'
        assert block['code'] == 'kb_search_failed'
        assert 'rag exploded' in block['detail']

    def test_empty_query_is_error_block(self, monkeypatch):
        inject_retrieve(monkeypatch, lambda u, q, k=5: [])
        block = run(tools.kb_search(None, query='   '))
        assert block['type'] == 'error'
        assert block['code'] == 'kb_search_query_missing'


class TestWebSearchTool:
    def test_injected_client_results_render_as_table(self):
        block = run(tools.web_search(None, query='nitride etch', client=_FakeClient()))
        assert block['type'] == 'table'
        assert block['rows'] == [[
            'SEMATECH e-handbook', 'https://example.com/etch', 'nitride etch rates'
        ]]
        assert block['results'][0]['url'] == 'https://example.com/etch'

    def test_degraded_without_config_is_error_block_not_crash(self, monkeypatch):
        monkeypatch.delenv('GOOGLE_API_KEY', raising=False)
        monkeypatch.delenv('GOOGLE_SEARCH_ENGINE_ID', raising=False)
        assert tools.web_search_available() is False
        block = run(tools.web_search(None, query='anything'))
        assert block['type'] == 'error'
        assert block['code'] == 'web_search_not_configured'
        assert 'GOOGLE_API_KEY' in block['detail']

    def test_backend_failure_is_error_block_never_raises(self):
        block = run(tools.web_search(
            None, query='q', client=_FakeClient(error=RuntimeError('DNS died'))))
        assert block['type'] == 'error'
        assert block['code'] == 'web_search_failed'
        assert 'DNS died' in block['detail']

    def test_empty_query_is_error_block(self):
        block = run(tools.web_search(None, query='', client=_FakeClient()))
        assert block['type'] == 'error'
        assert block['code'] == 'web_search_query_missing'

    def test_availability_true_when_keys_configured(self, monkeypatch):
        monkeypatch.setenv('GOOGLE_API_KEY', 'test-key')
        monkeypatch.setenv('GOOGLE_SEARCH_ENGINE_ID', 'test-engine')
        assert tools.web_search_available() is True


class TestExcursionWrapper:
    def test_accepts_json_string_metrics(self):
        block = run(tools.excursion_triage(None, metrics='{"yield_pct": 55}'))
        assert block['type'] == 'table'
        assert block['flags'] == ['YIELD_CRITICAL']

    def test_invalid_json_is_error_block(self):
        block = run(tools.excursion_triage(None, metrics='not json'))
        assert block['type'] == 'error'

    def test_non_mapping_metrics_is_error_block(self):
        block = run(tools.excursion_triage(None, metrics=42))
        assert block['type'] == 'error'

    def test_missing_metrics_is_error_block(self):
        block = run(tools.excursion_triage(None))
        assert block['type'] == 'error'
