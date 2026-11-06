from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.shortcuts import redirect
from django.urls import include, path

from apps.accounts import views as account_views


def home(request):
    """首页按角色跳转。"""
    if not request.user.is_authenticated:
        return redirect('login')
    if request.user.role in ('manager', 'admin'):
        return redirect('/admin/')
    return redirect('reports:report_list')


urlpatterns = [
    path('', home, name='home'),
    path('login/', auth_views.LoginView.as_view(
        template_name='accounts/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('password/', account_views.password_change, name='password_change'),
    path('consent/', include('apps.accounts.urls')),
    path('reports/', include('apps.reports.urls')),
    path('stats/', include('apps.stats.urls')),
    path('admin/', admin.site.urls),
]
