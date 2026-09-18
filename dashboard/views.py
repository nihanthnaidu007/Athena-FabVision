"""Server-rendered dashboard views: usage analytics and API-key management.

Every read goes through the user-scoped querysets from assistant.models
(``for_user``), so cross-user data is unreachable by construction; the
key lifecycle reuses PR #5's ``ApiKey.generate`` service -- no hashing
or validation is reimplemented here.
"""

from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login as auth_login
from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.db.models import Count
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from assistant.models import ApiKey, Document, MessageFeedback, Notebook, UsageEvent
from rag.ingestion import reingest_document as reingest_document_service

from .aggregates import breakdowns, chart_bars, feedback_summary, parse_days, usage_summary
from .forms import ApiKeyCreateForm
from .seed import seed_new_user_knowledge_base

NOTEBOOK_NAME_MAX = 200  # mirrors the model's CharField length


class LoginView(auth_views.LoginView):
    """The stock login view plus the signup toggle for its template.

    The login page links to self-registration only when the deployment
    allows it, so a private deployment (SIGNUPS_ENABLED=false) shows no
    dead-end link.
    """

    def get_context_data(self, **kwargs: object) -> dict:
        context = super().get_context_data(**kwargs)
        context['signups_enabled'] = settings.SIGNUPS_ENABLED
        return context


@require_http_methods(['GET', 'POST'])
def register(request: HttpRequest) -> HttpResponse:
    """Self-service account creation with a first-run seeded KB (v1.1 #3).

    On by default; a private deployment turns it off with
    SIGNUPS_ENABLED=false, which removes the route entirely (404) and
    hides the login-page link. A valid signup creates the account,
    seeds its knowledge base with the starter documents, and logs the
    user in, so the product works from the very first visit.
    """
    if not settings.SIGNUPS_ENABLED:
        raise Http404('Registration is not enabled on this deployment.')
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            seed_new_user_knowledge_base(user)
            auth_login(request, user)
            return redirect('chat-home')
    else:
        form = UserCreationForm()
    return render(request, 'registration/register.html', {'form': form})


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
    # Same window as the usage events: the feedback section answers "how
    # is this window's answering doing", not all-time sentiment.
    feedbacks = list(
        MessageFeedback.objects.for_user(request.user)
        .filter(created_at__gte=since)
        .select_related('message__conversation')
    )
    return render(
        request,
        'dashboard/usage.html',
        {
            'summary': summary,
            'breakdowns': breakdowns(events, days=days, now=timezone.now()),
            'chart': chart_bars(summary['daily']),
            'feedback': feedback_summary(feedbacks),
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
    notebooks = list(Notebook.objects.for_user(request.user).order_by('name'))
    return render(
        request,
        'dashboard/documents.html',
        {'documents': documents, 'notebooks': notebooks},
    )


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


def _valid_notebook_name(name: str) -> bool:
    """A notebook name is non-empty and fits the model's CharField."""
    return 0 < len(name) <= NOTEBOOK_NAME_MAX


@login_required
@require_http_methods(['GET', 'POST'])
def notebooks_view(request: HttpRequest) -> HttpResponse:
    """List the user's notebooks with member counts; create from the form.

    Counts use distinct annotations because documents and conversations
    join from different FKs -- without ``distinct=True`` the cross join
    would multiply both numbers. Creation validates the name up front so
    the per-user uniqueness constraint surfaces as a readable banner
    instead of a 500.
    """
    if request.method == 'POST':
        name = str(request.POST.get('name') or '').strip()
        if not _valid_notebook_name(name):
            messages.error(request, 'A notebook name is required (up to 200 characters).')
        elif Notebook.objects.for_user(request.user).filter(name=name).exists():
            messages.error(request, f'You already have a notebook named "{name}".')
        else:
            Notebook.objects.create(user=request.user, name=name)
            messages.success(request, f'Created notebook "{name}".')
        return redirect('dashboard:notebooks')
    notebooks = (
        Notebook.objects.for_user(request.user)
        .annotate(
            document_count=Count('documents', distinct=True),
            conversation_count=Count('conversations', distinct=True),
        )
        .order_by('name')
    )
    return render(request, 'dashboard/notebooks.html', {'notebooks': notebooks})


@login_required
@require_POST
def rename_notebook(request: HttpRequest, notebook_id: int) -> HttpResponse:
    """Rename one of the user's notebooks; other users' notebooks 404 here."""
    notebook = get_object_or_404(Notebook.objects.for_user(request.user), pk=notebook_id)
    name = str(request.POST.get('name') or '').strip()
    if not _valid_notebook_name(name):
        messages.error(request, 'A notebook name is required (up to 200 characters).')
    elif (
        Notebook.objects.for_user(request.user).filter(name=name).exclude(pk=notebook.pk).exists()
    ):
        messages.error(request, f'You already have a notebook named "{name}".')
    else:
        notebook.name = name
        notebook.save(update_fields=['name'])
        messages.success(request, f'Renamed notebook to "{name}".')
    return redirect('dashboard:notebooks')


@login_required
@require_POST
def delete_notebook(request: HttpRequest, notebook_id: int) -> HttpResponse:
    """Delete a notebook: members survive, unscoped (SET_NULL), never deleted."""
    notebook = get_object_or_404(Notebook.objects.for_user(request.user), pk=notebook_id)
    name = notebook.name
    notebook.delete()
    messages.success(
        request,
        f'Deleted notebook "{name}". Its documents and conversations were kept, now unscoped.',
    )
    return redirect('dashboard:notebooks')


@login_required
@require_POST
def assign_document_notebook(request: HttpRequest, doc_id: int) -> HttpResponse:
    """Assign one of the user's documents to a notebook; empty value unassigns.

    Both sides are ownership-checked: the document through ``for_user``,
    the target notebook through its own scoped queryset, so a foreign
    notebook id 404s instead of silently linking across users.
    """
    document = get_object_or_404(Document.objects.for_user(request.user), pk=doc_id)
    raw = request.POST.get('notebook_id')
    if raw in (None, ''):
        document.notebook = None
        document.save(update_fields=['notebook'])
        messages.success(request, f'"{document.original_filename}" unassigned from notebooks.')
        return redirect('dashboard:documents')
    notebook = get_object_or_404(Notebook.objects.for_user(request.user), pk=raw)
    document.notebook = notebook
    document.save(update_fields=['notebook'])
    messages.success(
        request, f'"{document.original_filename}" moved to notebook "{notebook.name}".'
    )
    return redirect('dashboard:documents')
