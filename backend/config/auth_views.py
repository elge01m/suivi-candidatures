"""Session endpoints use Django views so CSRF middleware also checks anonymous POSTs."""

import json

from django.contrib.auth import authenticate, login as session_login, logout as session_logout
from django.http import HttpResponse, JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET, require_POST


def user_data(user):
    return {'id': user.pk, 'username': user.get_username()}


@never_cache
@require_GET
@ensure_csrf_cookie
def csrf(request):
    return JsonResponse({'detail': 'Cookie CSRF initialise.'})


@never_cache
@require_POST
def login(request):
    if request.content_type != 'application/json':
        return JsonResponse({'detail': 'Un corps JSON est requis.'}, status=415)
    try:
        payload = json.loads(request.body)
    except (ValueError, UnicodeDecodeError):
        return JsonResponse({'detail': 'JSON invalide.'}, status=400)
    if not isinstance(payload, dict):
        return JsonResponse({'detail': 'Objet JSON requis.'}, status=400)
    username, password = payload.get('username'), payload.get('password')
    if not isinstance(username, str) or not isinstance(password, str) or not username or not password:
        return JsonResponse({'detail': 'Identifiant et mot de passe requis.'}, status=400)
    user = authenticate(request, username=username, password=password)
    if user is None:
        return JsonResponse({'detail': 'Identifiants incorrects.'}, status=401)
    session_login(request, user)
    return JsonResponse(user_data(user))


@never_cache
@require_POST
def logout(request):
    session_logout(request)
    return HttpResponse(status=204)


@never_cache
@require_GET
def me(request):
    if not request.user.is_authenticated:
        return JsonResponse({'detail': 'Authentification requise.'}, status=401)
    return JsonResponse(user_data(request.user))
