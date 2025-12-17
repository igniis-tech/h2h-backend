# from django.contrib import admin
# from django.urls import path, include
# from django.conf import settings
# from django.conf.urls.static import static
# from django.contrib.staticfiles.urls import staticfiles_urlpatterns

# urlpatterns = [
#     path("admin/", admin.site.urls),
#     path("api/", include("h2h.urls")),
# ]
# urlpatterns += staticfiles_urlpatterns()
# if settings.DEBUG:
#     urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)



from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static
from django.contrib.staticfiles.urls import staticfiles_urlpatterns
from django.views.generic import RedirectView  # ← add this

urlpatterns = [
    # Redirect root to /api/
    path("", RedirectView.as_view(url="/api/", permanent=False)),
    # Also redirect /api (no trailing slash) → /api/
    path("api", RedirectView.as_view(url="/api/", permanent=False)),

    path("admin/", admin.site.urls),
    path("api/", include("h2h.urls")),
]

urlpatterns += staticfiles_urlpatterns()
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
# Force reload
