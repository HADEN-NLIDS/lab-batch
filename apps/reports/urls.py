from django.urls import path

from apps.reports import views

app_name = 'reports'

urlpatterns = [
    path('', views.report_list, name='report_list'),
    path('upload/', views.upload, name='upload'),
    path('upload/confirm/', views.upload_confirm, name='upload_confirm'),
    path('upload/pending/', views.upload_pending, name='upload_pending'),
    path('upload/pending/<int:pk>/assign/', views.assign_pending,
         name='assign_pending'),
    path('upload/pending/<int:pk>/image/', views.pending_image,
         name='pending_image'),
    path('<int:pk>/', views.report_detail, name='report_detail'),
    path('<int:pk>/reprocess/', views.reprocess, name='reprocess'),
    path('<int:pk>/delete/', views.delete_report, name='delete_report'),
    path('<int:pk>/page/<int:page_no>/image/', views.report_image,
         name='report_image'),
]
