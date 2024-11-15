from django.views.static import serve
from django.conf import settings
from django.urls import re_path

def home_view(request):
    return HttpResponse('<h1>Welcome to Athena AI Agent</h1><p>Go to <a href="/agent/">Agent</a> to interact with the AI.</p>')

urlpatterns = [
    path('admin/', admin.site.urls),
    path('agent/', include('agent.urls')),
    path('', home_view, name='home'),
    re_path(r'^favicon\.ico$', serve, {'path': 'favicon.ico', 'document_root': settings.STATIC_ROOT}),
]