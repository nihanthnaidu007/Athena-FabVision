from django.contrib.auth import views as auth_views
from django.urls import path

from . import views

app_name = 'dashboard'

urlpatterns = [
    path('', views.usage_view, name='usage'),
    path('keys/', views.keys_view, name='keys'),
    path('keys/<int:key_id>/revoke/', views.revoke_key, name='revoke-key'),
    path('documents/', views.documents_view, name='documents'),
    path('documents/<int:doc_id>/delete/', views.delete_document, name='delete-document'),
    path('documents/<int:doc_id>/reingest/', views.reingest_document, name='reingest-document'),
    # Notebooks (v1.1 #5): grouping layer over documents and conversations.
    path('notebooks/', views.notebooks_view, name='notebooks'),
    path('notebooks/<int:notebook_id>/rename/', views.rename_notebook, name='rename-notebook'),
    path('notebooks/<int:notebook_id>/delete/', views.delete_notebook, name='delete-notebook'),
    path(
        'documents/<int:doc_id>/notebook/',
        views.assign_document_notebook,
        name='assign-document-notebook',
    ),
    # Login/logout for the dashboard's login_required pages; the default
    # Auth flow for the login_required pages: settings.LOGIN_URL
    # defaults to /dashboard/accounts/login/ (this mount). The login
    # view is a thin subclass that also tells the template whether the
    # deployment allows self-registration.
    path('accounts/login/', views.LoginView.as_view(), name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(), name='logout'),
    # Self-registration (v1.1 #3): linked from the login page when
    # SIGNUPS_ENABLED (the default); the view 404s when disabled.
    path('accounts/register/', views.register, name='register'),
]
