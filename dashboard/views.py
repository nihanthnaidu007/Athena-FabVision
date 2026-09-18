"""Server-rendered dashboard views: usage analytics and API-key management.

Every read goes through the user-scoped querysets from assistant.models
(``for_user``), so cross-user data is unreachable by construction; the
key lifecycle reuses PR #5's ``ApiKey.generate`` service -- no hashing
or validation is reimplemented here.
"""

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db.models import Count
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from assistant.models import ApiKey, Document, UsageEvent
from rag.ingestion import reingest_document as reingest_document_service

from .aggregates import chart_bars, parse_days, usage_summary
from .forms import ApiKeyCreateForm


@login_required
def usage_view(request: HttpRequest) -> HttpResponse:
    """Per-user usage: requests, tokens, latency over a 7 or 30 day window."""
    days = parse_days(request.GET.get('days'))
    since = timezone.now() - timedelta(days=days)
    events = list(
        UsageEvent.objects.for_user(request.user)
        .filter(created_at__gte=since)
        .order_by('created_at')
    )
    summary = usage_summary(events, days=days, now=timezone.now())
    return render(
        request,
        'dashboard/usage.html',
        {
            'summary': summary,
            'chart': chart_bars(summary['daily']),
            'window': days,
            'windows': (7, 30),
        },
    )


@login_required
def keys_view(request: HttpRequest) -> HttpResponse:
    """List the user's API keys and create new ones.

    A freshly created key's raw secret is embedded in exactly this one
    response; nothing about it is stored server-side besides the hash.
    """
    new_raw_key = None
    if request.method == 'POST':
        form = ApiKeyCreateForm(request.POST)
        if form.is_valid():
            _api_key, new_raw_key = ApiKey.generate(
                created_by=request.user,
                name=form.cleaned_data['name'],
                rate_limit_tier=form.cleaned_data['rate_limit_tier'],
            )
            form = ApiKeyCreateForm()  # reset after a successful create
    else:
        form = ApiKeyCreateForm()
    return _render_keys(request, form, new_raw_key)


def _render_keys(
    request: HttpRequest, form: ApiKeyCreateForm, new_raw_key: str | None = None
) -> HttpResponse:
    keys = list(ApiKey.objects.for_user(request.user).prefetch_related('usage_events'))
    key_items = [
        {
            'key': key,
            'last_used': max(
                (event.created_at for event in key.usage_events.all()), default=None
            ),
        }
        for key in keys
    ]
    return render(
        request,
        'dashboard/keys.html',
        {'form': form, 'key_items': key_items, 'new_raw_key': new_raw_key},
    )


@login_required
@require_POST
def revoke_key(request: HttpRequest, key_id: int) -> HttpResponse:
    """Revoke one of the user's own keys; other users' keys 404 here."""
    api_key = get_object_or_404(ApiKey.objects.for_user(request.user), pk=key_id)
    if api_key.revoked_at is None:
        api_key.revoked_at = timezone.now()
        api_key.save(update_fields=['revoked_at'])
    return redirect('dashboard:keys')


@login_required
def documents_view(request: HttpRequest) -> HttpResponse:
    """List the user's knowledge-base documents with their ingestion state.

    Status and failure_reason surface exactly what the ingestion pipeline
    recorded, so a failed upload is visible and fixable here instead of
    disappearing into a write-only store. Chunk counts come from one
    annotated query -- no per-row follow-ups.
    """
    documents = Document.objects.for_user(request.user).annotate(chunk_count=Count('chunks'))
    return render(request, 'dashboard/documents.html', {'documents': documents})


@login_required
@require_POST
def delete_document(request: HttpRequest, doc_id: int) -> HttpResponse:
    """Delete one of the user's documents; other users' documents 404 here.

    Chunk has no user FK -- chunks are scoped through their parent and
    cascade on delete, so a document and its retrieval chunks always
    disappear together.
    """
    document = get_object_or_404(Document.objects.for_user(request.user), pk=doc_id)
    filename = document.original_filename
    document.delete()
    messages.success(request, f'Deleted "{filename}" and its retrieval chunks.')
    return redirect('dashboard:documents')


@login_required
@require_POST
def reingest_document(request: HttpRequest, doc_id: int) -> HttpResponse:
    """Re-run ingestion for a pending or failed document; ready ones stay put.

    Every outcome is reported honestly: ready reports the chunk count,
    pending means the document is stored but waiting for an embeddings
    key (zero-key deployments), failed repeats the recorded reason. The
    re-ingest service clears stale chunks, so the transition is
    idempotent however many times the user retries.
    """
    document = get_object_or_404(Document.objects.for_user(request.user), pk=doc_id)
    if document.status not in (Document.Status.PENDING, Document.Status.FAILED):
        messages.info(
            request, f'"{document.original_filename}" is already ready; nothing to re-ingest.'
        )
        return redirect('dashboard:documents')
    result = reingest_document_service(document)
    if result.status == Document.Status.READY:
        messages.success(
            request, f'Re-ingested "{document.original_filename}": {result.detail}'
        )
    elif result.status == Document.Status.PENDING:
        messages.warning(
            request, f'"{document.original_filename}" is not processed yet: {result.detail}'
        )
    else:
        messages.error(
            request, f'Re-ingest of "{document.original_filename}" failed: {result.detail}'
        )
    return redirect('dashboard:documents')
