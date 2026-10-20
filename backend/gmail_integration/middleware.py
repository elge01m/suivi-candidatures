from django.http import JsonResponse
from django.utils.cache import add_never_cache_headers


class OAuthPrivacyMiddleware:
    """Also sanitize framework/middleware failures occurring outside OAuth views."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith('/api/gmail/'):
            if response.status_code >= 400 and not getattr(response, 'gmail_safe_response', False):
                safe_response = JsonResponse(
                    {'detail': 'Requête Gmail refusée ou indisponible. Vérifiez votre session et recommencez.'},
                    status=response.status_code,
                )
                if 'Allow' in response:
                    safe_response['Allow'] = response['Allow']
                response = safe_response
            response['Referrer-Policy'] = 'no-referrer'
            add_never_cache_headers(response)
        return response
