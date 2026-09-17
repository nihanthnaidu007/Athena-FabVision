from django.urls import path

from . import views

urlpatterns = [
    path('', views.voice_home, name='voice-home'),
    path('token/', views.VoiceTokenView.as_view(), name='voice-token'),
]
