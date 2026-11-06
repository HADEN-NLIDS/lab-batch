from django.urls import path

from apps.accounts import views

app_name = 'accounts'

urlpatterns = [
    path('', views.consent_view, name='consent'),
    path('revoke/', views.revoke_consent, name='revoke'),
]
