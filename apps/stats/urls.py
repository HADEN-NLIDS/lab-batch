from django.urls import path

from apps.stats import views

app_name = 'stats'

urlpatterns = [
    path('', views.dashboard, name='dashboard'),
]
