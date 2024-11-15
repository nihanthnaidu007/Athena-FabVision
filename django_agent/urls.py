from django.contrib import admin
from django.urls import path, include
from django.http import HttpResponse

def home_view(request):
    return HttpResponse('<h1>Welcome to Athena AI Agent</h1><p>Go to <a href="/agent/">Agent</a> to interact with the AI.</p>')

urlpatterns = [
    path('admin/', admin.site.urls),
    path('agent/', include('agent.urls')),
    path('', home_view, name='home'),
]
