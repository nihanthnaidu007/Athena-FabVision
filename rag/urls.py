"""Knowledge-base routes: included under /kb/ by django_agent.urls."""

from django.urls import path

from .views import KbDocumentUploadView

urlpatterns = [
    path('documents/', KbDocumentUploadView.as_view(), name='kb-document-upload'),
]
