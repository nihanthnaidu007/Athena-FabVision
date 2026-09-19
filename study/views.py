"""Flashcard generation endpoint (spec #6): one budgeted LLM call per POST.

The endpoint is deliberately small: it resolves the caller's notebook
(foreign ids 404 through ``for_user``), runs the generation service,
and lands every outcome -- success, no LLM key, empty notebook,
schema garbage -- as a readable banner on the dashboard's review
queue. Nothing here parses the model's output; that is
``study.generation``'s validated contract.
"""

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from assistant.models import Notebook

from .generation import (
    GenerationEmpty,
    GenerationError,
    GenerationUnavailable,
    generate_flashcards_for_notebook,
)


@login_required
@require_POST
def generate_flashcards(request: HttpRequest) -> HttpResponse:
    """Generate flashcards from one of the user's notebooks; land on the queue."""
    try:
        notebook_id = int(request.POST.get('notebook_id', ''))
    except ValueError:
        messages.error(request, 'Choose a notebook to generate flashcards from.')
        return redirect('dashboard:review')
    notebook = get_object_or_404(Notebook.objects.for_user(request.user), pk=notebook_id)
    try:
        cards = generate_flashcards_for_notebook(user=request.user, notebook=notebook)
    except GenerationUnavailable:
        messages.warning(
            request,
            'Flashcard generation needs an LLM key on this deployment; '
            'grading and reviews still work offline.',
        )
    except GenerationEmpty:
        messages.warning(
            request,
            f'Notebook "{notebook.name}" has no ready documents to ground on -- '
            'assign ready documents to it first.',
        )
    except GenerationError as exc:
        messages.error(request, f'Flashcard generation failed: {exc}')
    else:
        messages.success(request, f'Generated {len(cards)} flashcards from "{notebook.name}".')
    return redirect('dashboard:review')
