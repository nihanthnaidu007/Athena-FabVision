"""Test-only URLConf: a view that always raises, for middleware tests.

Not part of the application URLConf -- this exists purely so tests can
drive Django's real middleware chain (which wires process_exception)
against a raising view without adding a product endpoint.
"""

from django.http import HttpResponse
from django.urls import path


def raising_view(request):
    raise RuntimeError('boom')


def ok_view(request):
    return HttpResponse('ok')


urlpatterns = [
    path('boom/', raising_view, name='boom'),
    path('ok/', ok_view, name='ok'),
]
