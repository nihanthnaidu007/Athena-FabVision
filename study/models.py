"""Study models: flashcards and their spaced-review audit log.

Ownership contract (assistant.models): every row belongs to exactly one
user. A Flashcard carries its own ``user`` FK because it is authored
data, like MessageFeedback; ReviewLog is scoped through its card and
also carries ``user`` so per-user review history stays a flat query.
The notebook link is a grouping convenience only: deleting the notebook
keeps the cards (SET_NULL) -- consistent with how documents and
conversations leave a notebook -- they just lose the grouping.
"""

from django.conf import settings
from django.db import models

from assistant.models import UserScopedManager


class Flashcard(models.Model):
    """One question/answer pair with its SM-2 scheduling state.

    ``repetitions``/``interval_days``/``ease`` are owned by
    ``study.scheduler.schedule`` -- nothing else mutates them. A new
    card is due immediately (``due_at`` set at creation), so it surfaces
    in the review queue the moment it is generated.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='flashcards'
    )
    # Which notebook the card was generated from; None for cards from
    # other future sources. Deleting the notebook unassigns (SET_NULL).
    notebook = models.ForeignKey(
        'assistant.Notebook',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='flashcards',
    )
    question = models.TextField()
    answer = models.TextField()
    # SM-2 state; study.scheduler.schedule owns the transitions.
    repetitions = models.PositiveIntegerField(default=0)
    interval_days = models.PositiveIntegerField(default=0)
    ease = models.FloatField(default=2.5)
    due_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserScopedManager()

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', 'due_at'])]

    def __str__(self):
        return f'Flashcard {self.pk}: {self.question[:50]}'


class ReviewLog(models.Model):
    """One graded review of a flashcard -- the audit trail of the schedule.

    The grading view only ever reaches cards through ``for_user``, so
    ``card.user`` and ``user`` are always the same user; the explicit FK
    keeps the authored-data rule of this codebase and makes per-user
    review history a single-index query.
    """

    card = models.ForeignKey(Flashcard, on_delete=models.CASCADE, related_name='review_logs')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='review_logs'
    )
    grade = models.PositiveSmallIntegerField()
    previous_interval_days = models.PositiveIntegerField(default=0)
    next_interval_days = models.PositiveIntegerField(default=0)
    reviewed_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = UserScopedManager()

    class Meta:
        ordering = ['-reviewed_at']
        indexes = [models.Index(fields=['user', '-reviewed_at'])]

    def __str__(self):
        return f'grade {self.grade} on card {self.card_id} by user {self.user_id}'
