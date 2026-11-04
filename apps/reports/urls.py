from django.urls import path

from apps.reports import views

app_name = 'reports'

urlpatterns = [
    path('', views.report_list, name='report_list'),
    path('upload/', views.upload, name='upload'),
    path('<int:pk>/', views.report_detail, name='report_detail'),
    path('<int:pk>/reprocess/', views.reprocess, name='reprocess'),
    path('<int:pk>/page/<int:page_no>/image/', views.report_image,
         name='report_image'),
]
