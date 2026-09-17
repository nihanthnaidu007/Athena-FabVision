"""Web-search client tests: pure response parsing + injectable transport.

No test performs a real HTTP call — the transport is a fake, matching the
repo rule that tests never hit real external APIs.
"""

import asyncio

import pytest

from fabtools.search import (
    CSE_ENDPOINT,
    GoogleCSEClient,
    WebSearchError,
    _parse_cse_response,
    default_web_search_client,
)


def run(coro):
    return asyncio.run(coro)


class TestParseCseResponse:
    def test_items_map_to_web_results(self):
        payload = {'items': [{'title': 't', 'link': 'https://x', 'snippet': 's'}]}
        assert _parse_cse_response(payload, 5) == [
            {'title': 't', 'url': 'https://x', 'snippet': 's'}
        ]

    def test_k_truncates_results(self):
        payload = {'items': [{'title': str(i)} for i in range(10)]}
        results = _parse_cse_response(payload, 3)
        assert len(results) == 3

    def test_missing_fields_become_empty_strings(self):
        payload = {'items': [{}]}
        assert _parse_cse_response(payload, 5) == [{'title': '', 'url': '', 'snippet': ''}]

    def test_non_mapping_items_are_skipped(self):
        payload = {'items': ['garbage', {'title': 't'}]}
        assert len(_parse_cse_response(payload, 5)) == 1

    def test_error_payload_raises_web_search_error(self):
        with pytest.raises(WebSearchError, match='quota exceeded'):
            _parse_cse_response({'error': {'message': 'quota exceeded'}}, 5)

    def test_empty_payload_returns_empty_list(self):
        assert _parse_cse_response({}, 5) == []


class TestGoogleCSEClient:
    def test_transport_receives_endpoint_and_params(self):
        captured = {}

        async def transport(url, params, timeout):
            captured.update(url=url, params=dict(params))
            return {'items': [{'title': 'ok'}]}

        client = GoogleCSEClient('key', 'engine', transport=transport)
        results = run(client.search('gaas etch', k=3))
        assert captured['url'] == CSE_ENDPOINT
        assert captured['params']['q'] == 'gaas etch'
        assert captured['params']['key'] == 'key'
        assert captured['params']['cx'] == 'engine'
        assert captured['params']['num'] == '3'
        assert results == [{'title': 'ok', 'url': '', 'snippet': ''}]

    def test_num_clamped_to_google_maximum(self):
        captured = {}

        async def transport(url, params, timeout):
            captured.update(params=dict(params))
            return {}

        client = GoogleCSEClient('key', 'engine', transport=transport)
        run(client.search('q', k=50))
        assert captured['params']['num'] == '10'

    def test_transport_failure_propagates_as_web_search_error(self):
        async def transport(url, params, timeout):
            raise WebSearchError('boom')

        client = GoogleCSEClient('key', 'engine', transport=transport)
        with pytest.raises(WebSearchError, match='boom'):
            run(client.search('q'))


class TestDefaultClient:
    def test_none_without_keys(self, monkeypatch):
        monkeypatch.delenv('GOOGLE_API_KEY', raising=False)
        monkeypatch.delenv('GOOGLE_SEARCH_ENGINE_ID', raising=False)
        assert default_web_search_client() is None

    def test_client_built_when_both_keys_present(self, monkeypatch):
        monkeypatch.setenv('GOOGLE_API_KEY', 'k')
        monkeypatch.setenv('GOOGLE_SEARCH_ENGINE_ID', 'e')
        client = default_web_search_client()
        assert isinstance(client, GoogleCSEClient)

    def test_missing_engine_id_still_degrades(self, monkeypatch):
        monkeypatch.setenv('GOOGLE_API_KEY', 'k')
        monkeypatch.delenv('GOOGLE_SEARCH_ENGINE_ID', raising=False)
        assert default_web_search_client() is None
