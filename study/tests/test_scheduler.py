"""SM-2 scheduler unit tests across every grade path (spec #6).

``schedule`` is pure and offline: these tests run against unsaved
Flashcard instances (no database) with a fixed ``now``, so every
interval, ease, and due-date value below is exact, not approximate.
"""

import datetime

from django.test import SimpleTestCase

from study.models import Flashcard
from study.scheduler import EASE_FLOOR, schedule

NOW = datetime.datetime(2026, 9, 19, 12, 0, tzinfo=datetime.timezone.utc)


def make_card(**overrides) -> Flashcard:
    """An unsaved mid-life card: three successes, ease 2.5, interval 15 days."""
    fields = {
        'question': 'What does an edge ring indicate?',
        'answer': 'A die pattern concentrated at the wafer edge.',
        'repetitions': 3,
        'interval_days': 15,
        'ease': 2.5,
        'due_at': NOW,
    }
    fields.update(overrides)
    return Flashcard(**fields)


class SchedulerValidationTests(SimpleTestCase):
    def test_out_of_range_grades_raise_value_error(self):
        for grade in (-1, 6, 99):
            with self.subTest(grade=grade):
                with self.assertRaises(ValueError):
                    schedule(make_card(), grade, NOW)

    def test_boundary_grades_are_valid(self):
        for grade in (0, 5):
            with self.subTest(grade=grade):
                schedule(make_card(), grade, NOW)  # must not raise


class SuccessGradeTests(SimpleTestCase):
    """Grades 3-5: repetitions climb, intervals grow 1 -> 6 -> n * ease."""

    def test_first_success_schedules_one_day_out(self):
        card = make_card(repetitions=0, interval_days=0)
        schedule(card, 3, NOW)
        self.assertEqual(card.repetitions, 1)
        self.assertEqual(card.interval_days, 1)
        self.assertEqual(card.due_at, NOW + datetime.timedelta(days=1))

    def test_second_success_schedules_six_days_out(self):
        card = make_card(repetitions=1, interval_days=1)
        schedule(card, 4, NOW)
        self.assertEqual(card.repetitions, 2)
        self.assertEqual(card.interval_days, 6)
        self.assertEqual(card.due_at, NOW + datetime.timedelta(days=6))

    def test_third_success_multiplies_by_ease(self):
        card = make_card(repetitions=2, interval_days=6, ease=2.5)
        schedule(card, 5, NOW)
        self.assertEqual(card.repetitions, 3)
        self.assertEqual(card.interval_days, round(6 * card.ease))
        self.assertEqual(card.due_at, NOW + datetime.timedelta(days=card.interval_days))

    def test_grade_five_raises_ease_by_tenth(self):
        card = make_card(ease=2.5)
        schedule(card, 5, NOW)
        self.assertAlmostEqual(card.ease, 2.6)

    def test_grade_four_raises_ease_by_four_hundredths(self):
        card = make_card(ease=2.5)
        schedule(card, 4, NOW)
        # Spec sketch formula: 0.1 - 1*0.08 + 1*0.02 = +0.04.
        self.assertAlmostEqual(card.ease, 2.54)

    def test_grade_three_lowers_ease(self):
        card = make_card(ease=2.5)
        schedule(card, 3, NOW)
        # SM-2 EF update for q=3: 0.1 - 2*0.08 - 0 = -0.06.
        self.assertAlmostEqual(card.ease, 2.44)


class LapseGradeTests(SimpleTestCase):
    """Grades 0-2: the card returns tomorrow and the ease drops."""

    def test_first_review(self):
        card = make_card(repetitions=0, interval_days=0)
        schedule(card, 2, NOW)
        self.assertEqual(card.repetitions, 0)
        self.assertEqual(card.interval_days, 1)
        self.assertEqual(card.due_at, NOW + datetime.timedelta(days=1))

    def test_lapse_resets_a_long_streak(self):
        card = make_card(repetitions=3, interval_days=15, ease=2.5)
        schedule(card, 1, NOW)
        self.assertEqual(card.repetitions, 0)
        self.assertEqual(card.interval_days, 1)
        self.assertAlmostEqual(card.ease, 2.3)

    def test_every_lapse_grade_drops_ease_by_two_tenths(self):
        for grade in (0, 1, 2):
            with self.subTest(grade=grade):
                card = make_card(ease=2.5)
                schedule(card, grade, NOW)
                self.assertAlmostEqual(card.ease, 2.3)

    def test_ease_never_drops_below_floor(self):
        card = make_card(ease=EASE_FLOOR)
        schedule(card, 0, NOW)
        self.assertEqual(card.ease, EASE_FLOOR)


class DueDateTests(SimpleTestCase):
    def test_due_date_is_exactly_now_plus_interval(self):
        card = make_card(repetitions=2, interval_days=6, ease=2.5)
        schedule(card, 5, NOW)
        self.assertEqual(card.due_at, NOW + datetime.timedelta(days=card.interval_days))

    def test_schedule_does_not_touch_the_question(self):
        card = make_card()
        schedule(card, 0, NOW)
        self.assertEqual(card.question, 'What does an edge ring indicate?')
        self.assertEqual(card.answer, 'A die pattern concentrated at the wafer edge.')
