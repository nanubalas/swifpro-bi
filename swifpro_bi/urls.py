from django.contrib import admin
from django.urls import path, include
from django.contrib.auth import views as auth_views
from django.http import HttpResponse


def health(_request):
    """Lightweight, unauthenticated liveness check for Render's health check.
    Returns 200 'ok', exposes no data, and does not touch the DB (so it never
    fails during a brief DB blip)."""
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("health/", health, name="health"),
    path("admin/", admin.site.urls),
    path("login/", auth_views.LoginView.as_view(template_name="auth/login.html"), name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("core.urls")),
]

# Custom 403 page (rendered when a PermissionDenied is raised)
handler403 = "core.views.permission_denied_view"

from django.conf import settings
from django.conf.urls.static import static

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
