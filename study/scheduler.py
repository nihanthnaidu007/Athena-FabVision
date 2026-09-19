"""Deterministic SM-2 spaced-repetition scheduling -- no LLM, no network.

``schedule`` is the single transition function the review path runs
after every grade (spec #6: the review path is offline and fully
unit-testable). Grades 0-2 are lapses -- the card comes back tomorrow
and the ease drops; grades 3-5 are successes -- the interval grows
1 -> 6 -> interval * ease days, per SuperMemo's SM-2, with the ease
floor of 1.3 from the spec sketch. The generation path (one budgeted
LLM call) lives in ``study.generation`` and never touches this module.
"""

from datetime import timedelta

from .models import Flashcard

GRADE_MIN = 0
GRADE_MAX = 5
#: Grades below this count as lapses; 3-5 are successes (SM-2 semantics).
LAPSE_THRESHOLD = 3
EASE_FLOOR = 1.3


def schedule(card: Flashcard, grade: int, now) -> Flashcard:
    """Apply one grade to ``card`` and return it with its next due date.

    grade: 0 (blackout) .. 5 (perfect). Deterministic, offline, fully
    unit-testable. Raises ValueError for an out-of-range grade so a
    view bug can never silently corrupt the schedule; the caller
    persists the returned card.
    """
    if not GRADE_MIN <= grade <= GRADE_MAX:
        raise ValueError(f'grade must be {GRADE_MIN}..{GRADE_MAX}, got {grade!r}')
    if grade < LAPSE_THRESHOLD:
        card.repetitions, card.interval_days, card.ease = 0, 1, max(EASE_FLOOR, card.ease - 0.2)
    else:
        card.ease = max(
            EASE_FLOOR,
            card.ease
            + (0.1 - (5 - grade) * 0.08 + (5 - grade) * (0.02 if grade == 4 else 0)),
        )
        card.repetitions += 1
        if card.repetitions == 1:
            card.interval_days = 1
        elif card.repetitions == 2:
            card.interval_days = 6
        else:
            card.interval_days = round(card.interval_days * card.ease)
    card.due_at = now + timedelta(days=card.interval_days)
    return card
