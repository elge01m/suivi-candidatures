from django.urls import path

from . import auth_views


app_name = 'auth_api'
urlpatterns = [
    path('csrf/', auth_views.csrf, name='csrf'),
    path('login/', auth_views.login, name='login'),
    path('logout/', auth_views.logout, name='logout'),
    path('me/', auth_views.me, name='me'),
]
