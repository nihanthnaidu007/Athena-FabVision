"""Flashcard generation service tests (spec #6).

The service is exercised against the shared zero-network FakeLLM, so
every schema-validation and degradation path here runs without any
network: valid payloads persist cards and meter the call, garbage
persists nothing but still meters the spend (it happened), and a
deployment with no LLM key fails before any call.
"""

import json
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from agent.tests.fakes import FakeLLM, delta, usage
from assistant.models import Chunk, Document, Notebook, UsageEvent
from study.generation import (
    CONTEXT_CHAR_LIMIT,
    GenerationEmpty,
    GenerationError,
    GenerationInvalidResponse,
    GenerationUnavailable,
    collect_notebook_context,
    generate_flashcards_for_notebook,
    parse_flashcards,
)
from study.models import Flashcard

User = get_user_model()

VALID_PAYLOAD = (
    '{"flashcards": [{"question": "  What is an edge ring?  ", "answer": "Die fails '
    'concentrated at the wafer edge."}]}'
)


def cards_payload(*pairs) -> str:
    return json.dumps({'flashcards': [{'question': q, 'answer': a} for q, a in pairs]})


class GenerationServiceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alice = User.objects.create_user('alice', password='pw12345')
        cls.bob = User.objects.create_user('bob', password='pw12345')
        cls.notebook = Notebook.objects.create(user=cls.alice, name='Lithography')

    def setUp(self):
        self.now = timezone.now()
        # Most tests need a grounded notebook: one ready document with text.
        self.make_document(self.alice, self.notebook, content='Photoresist spin speeds.')

    @staticmethod
    def make_document(user, notebook, *, status=Document.Status.READY, content=None):
        document = Document.objects.create(
            user=user,
            original_filename=f'{user.username}-notes.txt',
            file_type='txt',
            sha256=f'{user.pk}{status}{len(str(content))}'.ljust(64, '0'),
            notebook=notebook,
            status=status,
        )
        if content is not None:
            Chunk.objects.create(
                document=document, index=0, content=content, content_hash='0' * 64
            )
        return document

    @staticmethod
    def fake_llm(body: str, tokens: bool = True) -> FakeLLM:
        events = [delta(body)]
        if tokens:
            events.append(usage(tokens_in=30, tokens_out=12))
        return FakeLLM([events])

    # -- Valid generation ---------------------------------------------------

    def test_valid_payload_persists_cards_due_immediately(self):
        cards = generate_flashcards_for_notebook(
            user=self.alice, notebook=self.notebook, llm=self.fake_llm(VALID_PAYLOAD), now=self.now
        )

        self.assertEqual(len(cards), 1)
        card = cards[0]
        self.assertEqual(card.user, self.alice)
        self.assertEqual(card.notebook, self.notebook)
        self.assertEqual(card.question, 'What is an edge ring?')  # stripped
        self.assertEqual(card.repetitions, 0)
        self.assertEqual(card.due_at, self.now)
        self.assertTrue(Flashcard.objects.filter(user=self.alice).exists())

    def test_generation_is_one_llm_call(self):
        llm = self.fake_llm(cards_payload(('Q1', 'A1'), ('Q2', 'A2')))

        cards = generate_flashcards_for_notebook(
            user=self.alice, notebook=self.notebook, llm=llm, now=self.now
        )

        self.assertEqual(len(cards), 2)
        self.assertEqual(len(llm.calls), 1)  # the budget: one call, no more

    def test_generation_records_metered_usage_event(self):
        generate_flashcards_for_notebook(
            user=self.alice, notebook=self.notebook, llm=self.fake_llm(VALID_PAYLOAD), now=self.now
        )

        event = UsageEvent.objects.for_user(self.alice).get()
        self.assertEqual(event.kind, UsageEvent.Kind.STUDY)
        self.assertEqual(event.model, 'fake-model')
        self.assertEqual(event.tokens_in, 30)
        self.assertEqual(event.tokens_out, 12)

    def test_fenced_json_is_tolerated(self):
        fenced = '```json\n' + cards_payload(('Q', 'A')) + '\n```'
        cards = generate_flashcards_for_notebook(
            user=self.alice, notebook=self.notebook, llm=self.fake_llm(fenced), now=self.now
        )
        self.assertEqual(len(cards), 1)

    def test_context_sent_to_the_model_carries_the_notebook_text(self):
        self.make_document(self.alice, self.notebook, content='Photoresist spin speeds.')

        llm = self.fake_llm(VALID_PAYLOAD)
        generate_flashcards_for_notebook(
            user=self.alice, notebook=self.notebook, llm=llm, now=self.now
        )

        sent = ''.join(
            message['content'] for message in llm.calls[0]['messages']
            if message['role'] != 'system'
        )
        self.assertIn('Photoresist spin speeds.', sent)
        self.assertIn('alice-notes.txt', sent)

    # -- Honest degradation: garbage never persists ------------------------

    def test_invalid_json_persists_nothing_but_still_meters(self):
        llm = self.fake_llm('this is not json at all')

        with self.assertRaises(GenerationInvalidResponse):
            generate_flashcards_for_notebook(
                user=self.alice, notebook=self.notebook, llm=llm, now=self.now
            )
        self.assertEqual(Flashcard.objects.count(), 0)
        self.assertEqual(UsageEvent.objects.count(), 1)  # the spend happened

    def test_wrong_shape_is_rejected(self):
        for raw in ('{"cards": []}', '["flashcards"]', '{"flashcards": "many"}'):
            with self.subTest(raw=raw):
                with self.assertRaises(GenerationInvalidResponse):
                    parse_flashcards(raw)

    def test_empty_card_list_is_rejected(self):
        with self.assertRaises(GenerationInvalidResponse):
            parse_flashcards('{"flashcards": []}')

    def test_card_missing_fields_is_rejected(self):
        with self.assertRaises(GenerationInvalidResponse):
            parse_flashcards('{"flashcards": [{"question": "Q"}]}')

    def test_non_string_field_is_rejected(self):
        with self.assertRaises(GenerationInvalidResponse):
            parse_flashcards('{"flashcards": [{"question": 4, "answer": ["a"]}]}')

    def test_empty_after_strip_is_rejected(self):
        with self.assertRaises(GenerationInvalidResponse):
            parse_flashcards('{"flashcards": [{"question": "   ", "answer": "A"}]}')

    def test_overlong_answer_is_rejected(self):
        with self.assertRaises(GenerationInvalidResponse):
            parse_flashcards(
                '{"flashcards": [{"question": "Q", "answer": "' + 'x' * 2001 + '"}]}'
            )

    def test_more_than_twenty_cards_is_rejected(self):
        pairs = ((f'Q{i}', 'A') for i in range(21))
        with self.assertRaises(GenerationInvalidResponse):
            parse_flashcards(cards_payload(*pairs))

    def test_valid_parse_strips_and_bounds(self):
        parsed = parse_flashcards(VALID_PAYLOAD)
        self.assertEqual(
            parsed,
            [
                {
                    'question': 'What is an edge ring?',
                    'answer': 'Die fails concentrated at the wafer edge.',
                }
            ],
        )

    def test_llm_failure_is_wrapped_as_generation_error(self):
        llm = FakeLLM([[RuntimeError('upstream connection failed')]])
        with self.assertRaises(GenerationError) as ctx:
            generate_flashcards_for_notebook(
                user=self.alice, notebook=self.notebook, llm=llm, now=self.now
            )
        self.assertIn('model call failed', str(ctx.exception))
        self.assertEqual(Flashcard.objects.count(), 0)

    # -- Degradation before any call is spent --------------------------------

    def test_no_llm_raises_unavailable_without_spending_a_call(self):
        with mock.patch('study.generation.default_client', return_value=None):
            with self.assertRaises(GenerationUnavailable):
                generate_flashcards_for_notebook(
                    user=self.alice, notebook=self.notebook, now=self.now
                )

    def test_notebook_with_no_ready_documents_raises_empty(self):
        empty = Notebook.objects.create(user=self.alice, name='Empty')
        with mock.patch('study.generation.default_client') as default_client:
            with self.assertRaises(GenerationEmpty):
                generate_flashcards_for_notebook(
                    user=self.alice, notebook=empty, now=self.now
                )
        # Resolving the default client is free; spending a call is not.
        default_client.return_value.stream.assert_not_called()

    def test_pending_document_text_does_not_ground_generation(self):
        pending_notebook = Notebook.objects.create(user=self.alice, name='Pending only')
        self.make_document(
            self.alice, pending_notebook, status=Document.Status.PENDING, content='draft text'
        )
        with mock.patch('study.generation.default_client') as default_client:
            with self.assertRaises(GenerationEmpty):
                generate_flashcards_for_notebook(
                    user=self.alice, notebook=pending_notebook, now=self.now
                )
        default_client.return_value.stream.assert_not_called()

    def test_other_users_documents_never_ground_generation(self):
        # Alice's notebook is empty; Bob's has ready text. Bob's chunks must
        # never reach Alice's generation context -- and an empty notebook
        # degrades honestly instead of grounding on someone else's notes.
        lonely = Notebook.objects.create(user=self.alice, name='Lonely')
        bob_notebook = Notebook.objects.create(user=self.bob, name='Bob notes')
        self.make_document(self.bob, bob_notebook, content='Bob secret content')

        context = collect_notebook_context(lonely)

        self.assertEqual(context, '')

    # -- Context budget -------------------------------------------------------

    def test_context_is_capped_deterministically(self):
        for index in range(5):
            document = self.make_document(self.alice, self.notebook)
            Chunk.objects.create(
                document=document, index=index, content='x' * 500, content_hash=f'{index:064}'
            )

        context = collect_notebook_context(self.notebook, max_chars=1200)

        self.assertLessEqual(len(context), CONTEXT_CHAR_LIMIT)
        self.assertLessEqual(len(context), 1200 + len('[alice-notes.txt] '))

    def test_context_orders_documents_by_id_then_index(self):
        first = self.make_document(self.alice, self.notebook, content='first')
        second = self.make_document(self.alice, self.notebook, content='second')

        context = collect_notebook_context(self.notebook)

        self.assertIn(first.original_filename, context)
        self.assertIn(second.original_filename, context)
        self.assertLess(context.find('first'), context.find('second'))
