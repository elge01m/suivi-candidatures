from django.urls import path
from . import views

urlpatterns = [
    path('configuration/', views.configuration, name='sync-configuration'),
    path('runs/', views.enqueue, name='sync-enqueue'),
    path('imports/', views.initial_import, name='sync-initial-import'),
    path('imports/<uuid:import_id>/retry/', views.retry, name='sync-import-retry'),
    path('runs/<uuid:run_id>/', views.status, name='sync-status'),
]
