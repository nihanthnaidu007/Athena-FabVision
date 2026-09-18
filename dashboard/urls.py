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
    # Login/logout for the dashboard's login_required pages; the default
    # Auth flow for the login_required pages: settings.LOGIN_URL
    # defaults to /dashboard/accounts/login/ (this mount).
    path('accounts/login/', auth_views.LoginView.as_view(), name='login'),
    path('accounts/logout/', auth_views.LogoutView.as_view(), name='logout'),
]
