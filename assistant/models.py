"""User-scoped data models for Athena FabVision.

Ownership contract: every row belongs to exactly one user. User-facing
code reads through ``for_user(user)`` or a user's related manager (e.g.
``user.conversations``) -- both filter on the ownership FK at the
queryset layer, so cross-user reads are structurally impossible unless
code deliberately bypasses these accessors. Detail objects (Message,
Chunk) carry no user FK by design: they are reachable only through
their scoped parent.
"""

import hashlib
import secrets

from django.conf import settings
from django.db import models


def hash_raw_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest of a raw API key.

    Keys are 256-bit random tokens, so a fast unkeyed hash is safe from
    brute force; hashing also enables a unique-index lookup that stays
    constant time in the number of stored keys.
    """
    return hashlib.sha256(raw_key.encode('utf-8')).hexdigest()


class UserScopedQuerySet(models.QuerySet):
    """QuerySet for models owned via a ``user`` FK."""

    user_field = 'user'

    def for_user(self, user):
        """Only rows owned by ``user`` -- the base for all user-facing reads."""
        return self.filter(**{self.user_field: user})


class CreatorScopedQuerySet(UserScopedQuerySet):
    """User scoping for models whose ownership FK is ``created_by``."""

    user_field = 'created_by'


UserScopedManager = models.Manager.from_queryset(UserScopedQuerySet)
CreatorScopedManager = models.Manager.from_queryset(CreatorScopedQuerySet)


class Conversation(models.Model):
    """A chat thread; ``mode`` picks the agent's system preset."""

    class Mode(models.TextChoices):
        ASSISTANT = 'assistant'
        TUTOR = 'tutor'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='conversations'
    )
    title = models.CharField(max_length=200, blank=True, default='New conversation')
    # The agent loop resolves this to a system preset at prompt assembly
    # (agent/loop.py); the chat UI surfaces it as the composer toggle and
    # sidebar badge.
    mode = models.CharField(max_length=12, choices=Mode.choices, default=Mode.ASSISTANT)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserScopedManager()

    class Meta:
        ordering = ['-updated_at']
        indexes = [models.Index(fields=['user', '-updated_at'])]

    def __str__(self):
        return self.title or f'Conversation {self.pk}'


class Message(models.Model):
    """One turn in a conversation; scoped through its conversation."""

    class Role(models.TextChoices):
        USER = 'user'
        ASSISTANT = 'assistant'
        SYSTEM = 'system'
        TOOL = 'tool'

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name='messages'
    )
    role = models.CharField(max_length=12, choices=Role.choices)
    content = models.TextField()
    # [{"kind": "doc", "title": ..., "chunk_index": 3, "score": 0.82, "snippet": ...},
    #  {"kind": "tool", "tool": ..., "summary": ...}]
    sources = models.JSONField(default=list, blank=True)
    # Renderable UI blocks from tool calls: [{"type": "table"|"text"|"error", ...}]
    tool_blocks = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes = [models.Index(fields=['conversation', 'created_at'])]

    def __str__(self):
        return f'{self.role}: {self.content[:50]}'


class Document(models.Model):
    """An uploaded knowledge-base document with ingestion status."""

    class Status(models.TextChoices):
        PENDING = 'pending'
        READY = 'ready'
        FAILED = 'failed'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='documents'
    )
    original_filename = models.CharField(max_length=255)
    file_type = models.CharField(max_length=32, blank=True, default='')
    file = models.FileField(upload_to='documents/%Y/%m/')
    sha256 = models.CharField(max_length=64, db_index=True)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.PENDING)
    failure_reason = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserScopedManager()

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', '-created_at'])]

    def __str__(self):
        return self.original_filename


class Chunk(models.Model):
    """A retrieval chunk with its embedding and content hash (cache key)."""

    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name='chunks'
    )
    index = models.PositiveIntegerField()
    content = models.TextField()
    # Float list; cosine similarity computed in Python/SQL, swap for pgvector later.
    embedding = models.JSONField(default=list, blank=True)
    # sha256(content) -- the embedding cache key.
    content_hash = models.CharField(max_length=64, db_index=True)

    class Meta:
        ordering = ['index']
        constraints = [
            models.UniqueConstraint(
                fields=['document', 'index'], name='unique_chunk_index_per_document'
            )
        ]

    def __str__(self):
        return f'Chunk {self.index} of document {self.document_id}'


class ApiKey(models.Model):
    """A hashed API key for programmatic access, owned by its creator."""

    class RateTier(models.TextChoices):
        STANDARD = 'standard'
        HIGH = 'high'

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='api_keys'
    )
    name = models.CharField(max_length=100)
    prefix = models.CharField(max_length=8, db_index=True)  # shown in UI
    hashed_key = models.CharField(max_length=64, unique=True)
    rate_limit_tier = models.CharField(
        max_length=12, choices=RateTier.choices, default=RateTier.STANDARD
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    objects = CreatorScopedManager()

    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} ({self.prefix}...)'

    @classmethod
    def generate(cls, *, created_by, name, rate_limit_tier=RateTier.STANDARD):
        """Create a key and return ``(api_key, raw_key)``.

        The raw key is returned exactly once; only its SHA-256 hash is
        ever persisted.
        """
        raw_key = secrets.token_urlsafe(32)
        api_key = cls.objects.create(
            created_by=created_by,
            name=name,
            prefix=raw_key[:8],
            hashed_key=hash_raw_key(raw_key),
            rate_limit_tier=rate_limit_tier,
        )
        return api_key, raw_key

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


class UsageEvent(models.Model):
    """One recorded AI call, for the usage dashboard and analytics."""

    class Kind(models.TextChoices):
        CHAT = 'chat'
        API = 'api'
        VOICE = 'voice'
        TOOL = 'tool'

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='usage_events'
    )
    api_key = models.ForeignKey(
        ApiKey, on_delete=models.SET_NULL, null=True, blank=True, related_name='usage_events'
    )
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='usage_events',
    )
    message = models.ForeignKey(
        Message, on_delete=models.SET_NULL, null=True, blank=True, related_name='usage_events'
    )
    kind = models.CharField(max_length=20, choices=Kind.choices)
    model = models.CharField(max_length=60, blank=True, default='')
    tool_name = models.CharField(max_length=100, blank=True, default='')
    tokens_in = models.PositiveIntegerField(default=0)
    tokens_out = models.PositiveIntegerField(default=0)
    latency_ms = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = UserScopedManager()

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['user', '-created_at'])]

    def __str__(self):
        return f'{self.kind} event for user {self.user_id}'


class MessageFeedback(models.Model):
    """The user's rating of one assistant message (the per-message trust loop).

    Unlike the Message/Chunk detail rows, feedback is authored data and
    carries its own ``user`` FK. It is unique per (user, message): a
    changed mind upserts the same row, so the dashboard's ratios always
    reflect each user's current verdict -- never a click history.
    """

    class Value(models.TextChoices):
        UP = 'up'
        DOWN = 'down'

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name='feedback')
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='message_feedback'
    )
    value = models.CharField(max_length=4, choices=Value.choices)
    note = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = UserScopedManager()

    class Meta:
        ordering = ['-updated_at']
        constraints = [
            models.UniqueConstraint(
                fields=['message', 'user'], name='unique_feedback_per_user_message'
            )
        ]
        indexes = [models.Index(fields=['user', '-updated_at'])]

    def __str__(self):
        return f'{self.value} on message {self.message_id} by user {self.user_id}'
