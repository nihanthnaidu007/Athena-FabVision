from django.urls import path

from . import views

app_name = 'study'

urlpatterns = [
    path('generate/', views.generate_flashcards, name='generate'),
]
