from django.contrib import admin
from django.urls import path, include, re_path
from django.conf import settings
from django.conf.urls.static import static
from django.views.static import serve

from pos_app import views

urlpatterns = [
    path('admin/', views.admin_dashboard_view, name='admin_dashboard'),
    path('admin/sys/', admin.site.urls),
    path('', include('pos_app.urls')),
    # DEBUG=False (Prodüksiyon) modunda da statik ve medya dosyalarının kesintisiz sunulması
    re_path(r'^media/(?P<path>.*)$', serve, {'document_root': settings.MEDIA_ROOT}),
    re_path(r'^static/(?P<path>.*)$', serve, {'document_root': settings.STATIC_ROOT}),
]

if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)

admin.site.site_header = "Hİ MARKET Yönetim Paneli"
admin.site.site_title = "Hİ MARKET Back-Office"
admin.site.index_title = "Market ve Kasa Yönetimi"
