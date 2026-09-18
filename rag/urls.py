"""Knowledge-base routes: included under /kb/ by django_agent.urls."""

from django.urls import path

from .views import KbDocumentUploadView, KbExampleWaferView

urlpatterns = [
    path('documents/', KbDocumentUploadView.as_view(), name='kb-document-upload'),
    path(
        'documents/example-wafer/',
        KbExampleWaferView.as_view(),
        name='kb-document-example-wafer',
    ),
]
