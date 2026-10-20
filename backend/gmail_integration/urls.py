from django.urls import path

from . import import_views, views


app_name = 'gmail_integration'
urlpatterns = [
    path('connect/', views.connect, name='connect'),
    path('callback/', views.callback, name='callback'),
    path('imports/', import_views.create_import, name='import-create'),
    path('imports/<uuid:import_id>/', import_views.import_status, name='import-status'),
    path('imports/<uuid:import_id>/continue/', import_views.next_batch, name='import-continue'),
    path('messages/', import_views.delete_messages, name='message-purge'),
]
