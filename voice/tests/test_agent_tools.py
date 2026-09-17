"""Voice agent tool wiring against the shared core (fake LLM, fake registry).

No LiveKit server and no network: the session LLM is the scripted FakeLLM
from the shared agent fakes, and every registry/retrieval is a fake.
DB-touching tests follow the agent-suite pattern (transactional_db +
async_test) so sync_to_async threads can see committed rows.
"""

import pytest
from django.contrib.auth.models import User

from agent.loop import SYSTEM_PROMPT
from agent.tests.fakes import FakeLLM, async_test, delta, tool_call, usage
from assistant.models import Conversation
from voice.agent import (
    DEGRADED_NO_ACCOUNT,
    AthenaVoiceAgent,
    format_block_for_voice,
    format_retrieval_for_voice,
    registry_tool_for_voice,
    retrieve_for_voice,
    run_voice_turn,
)

from .fakes import FakeRegistry


@pytest.fixture
def user(transactional_db):
    return User.objects.create_user(username='nina', password='x')


@pytest.fixture
def conversation(transactional_db, user):
    return Conversation.objects.create(user=user, title='voice')


def table_block():
    return {
        'type': 'table',
        'title': 'Excursion triage',
        'summary': '2 zones flagged',
        'rows': [['Zone A', 'etch'], ['Zone B', 'dep']],
        'context': 'context for the model',
    }


@async_test
async def test_collapses_stream_deltas_into_the_answer(user, conversation):
    llm = FakeLLM([[delta('Bin 3 '), delta('yield '), usage(), delta('looks stable.')]])
    answer = await run_voice_turn(
        user=user,
        conversation=conversation,
        user_input='how is bin 3?',
        llm=llm,
        registry=FakeRegistry(),
    )
    assert answer == 'Bin 3 yield looks stable.'


@async_test
async def test_tool_rounds_run_and_the_final_answer_wins(user, conversation):
    llm = FakeLLM(
        [
            [tool_call('echo_tool', '{}')],
            [delta('Consulted the shared core.'), usage()],
        ]
    )
    registry = FakeRegistry()

    async def echo(user, **kwargs):
        return {'type': 'text', 'content': 'echo'}

    registry._tools['echo_tool'] = echo
    answer = await run_voice_turn(
        user=user,
        conversation=conversation,
        user_input='what changed?',
        llm=llm,
        registry=registry,
    )
    assert answer == 'Consulted the shared core.'
    assert len(llm.calls) == 2


@async_test
async def test_uses_the_error_event_when_no_text_was_produced(user, conversation):
    llm = FakeLLM([[RuntimeError('upstream connection failed')]])
    answer = await run_voice_turn(
        user=user,
        conversation=conversation,
        user_input='hello',
        llm=llm,
        registry=FakeRegistry(),
    )
    # Honest spoken error instead of an empty reply or a crash.
    assert answer != ''
    assert 'upstream connection failed' in answer


@async_test
async def test_degraded_llm_answers_through_the_error_contract(user, conversation):
    # llm=None means "resolve the deployment default"; with no key the loop
    # emits an error event, which the voice surface speaks.
    answer = await run_voice_turn(
        user=user,
        conversation=conversation,
        user_input='hello',
        llm=None,
        registry=FakeRegistry(),
    )
    assert 'not configured' in answer


@async_test
async def test_degraded_room_without_account_answers_honestly():
    llm = FakeLLM([[delta('should never be called')]])
    answer = await run_voice_turn(
        user=None,
        conversation=None,
        user_input='analyze this wafer',
        llm=llm,
        registry=FakeRegistry(),
    )
    assert answer == DEGRADED_NO_ACCOUNT
    assert llm.calls == []


@async_test
async def test_formats_cited_hits():
    async def retrieval(user, query, k=5):
        return [
            {'title': 'Etch spec', 'snippet': 'oxide ER 120 nm/min', 'score': 0.91},
            {'title': 'Runbook', 'snippet': 'check chamber pressure', 'score': 0.42},
        ]

    spoken = await retrieve_for_voice(
        user=None, query='etch rate', registry=FakeRegistry(retrieval=retrieval)
    )
    assert '[Etch spec] oxide ER 120 nm/min (relevance 0.91)' in spoken
    assert '[Runbook] check chamber pressure (relevance 0.42)' in spoken


@async_test
async def test_reports_when_no_hits():
    async def retrieval(user, query, k=5):
        return []

    spoken = await retrieve_for_voice(
        user=None, query='anything', registry=FakeRegistry(retrieval=retrieval)
    )
    assert 'No knowledge-base context found' in spoken


@async_test
async def test_reports_when_retrieval_unavailable():
    spoken = await retrieve_for_voice(user=None, query='etch', registry=FakeRegistry())
    assert 'not available' in spoken


@async_test
async def test_survives_a_failing_retrieval_source():
    async def retrieval(user, query, k=5):
        raise RuntimeError('embedding service down')

    spoken = await retrieve_for_voice(
        user=None, query='etch', registry=FakeRegistry(retrieval=retrieval)
    )
    assert 'failed' in spoken


def test_empty_retrieval_formatter():
    assert (
        format_retrieval_for_voice([]) == 'No knowledge-base context found for that query.'
    )


@async_test
async def test_speaks_a_table_block():
    async def triage(user, **kwargs):
        return table_block()

    registry = FakeRegistry(tools={'excursion_triage': triage})
    spoken = await registry_tool_for_voice(user=None, name='excursion_triage', registry=registry)
    assert 'Excursion triage: 2 zones flagged (2 result(s)).' in spoken
    assert 'Zone A, etch' in spoken


@async_test
async def test_unavailable_tool_spoken_as_error():
    registry = FakeRegistry()
    spoken = await registry_tool_for_voice(user=None, name='web_search', registry=registry)
    assert 'not available' in spoken


def test_error_block_spoken():
    spoken = format_block_for_voice(
        {'type': 'error', 'title': 'Tool failed', 'error': 'wafer file not found'}
    )
    assert spoken == 'Tool failed: wafer file not found.'


def test_wafer_map_block_spoken():
    assert format_block_for_voice({'type': 'wafer_map', 'summary': '5 dies failing'}) == (
        '5 dies failing'
    )


def test_text_block_content_spoken():
    assert format_block_for_voice({'type': 'text', 'content': 'all good'}) == 'all good'


def test_collects_the_three_function_tools():
    agent = AthenaVoiceAgent()
    for name in ('consult_athena', 'search_fab_knowledge', 'web_search'):
        assert getattr(agent, name, None) is not None, name


def test_instructions_come_from_the_shared_core():
    assert AthenaVoiceAgent().instructions == SYSTEM_PROMPT
    assert AthenaVoiceAgent(instructions='custom').instructions == 'custom'


@async_test
async def test_search_fab_knowledge_routes_through_registry_retrieval(user):
    async def retrieval(user, query, k=5):
        return [{'title': 'Spec', 'snippet': '120 nm/min', 'score': 0.9}]

    agent = AthenaVoiceAgent(user=user, registry=FakeRegistry(retrieval=retrieval))
    spoken = await agent.search_fab_knowledge(None, query='etch rate')
    assert '[Spec] 120 nm/min' in spoken


@async_test
async def test_web_search_routes_through_the_registry(user):
    async def web_search(user, **kwargs):
        return {'type': 'text', 'content': 'top result: fab forum'}

    registry = FakeRegistry(tools={'web_search': web_search})
    agent = AthenaVoiceAgent(user=user, registry=registry)
    spoken = await agent.web_search(None, query='latest etch news')
    assert spoken == 'top result: fab forum'
    assert registry.executed == [('web_search', {'query': 'latest etch news'})]
