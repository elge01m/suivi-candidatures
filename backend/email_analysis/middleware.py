from django.http import JsonResponse
from django.utils.cache import add_never_cache_headers


class AnalysisPrivacyMiddleware:
    """Prevent DEBUG pages/body logging from exposing model input or credentials."""
    def __init__(self, get_response):
        self.get_response = get_response

    def process_exception(self, request, exception):
        if request.path.startswith(('/api/analyses/', '/api/propositions/', '/api/sync/')):
            return JsonResponse({'code': 'internal_error', 'detail': 'Analyse indisponible.'}, status=503)

    def __call__(self, request):
        response = self.get_response(request)
        if request.path.startswith(('/api/analyses/', '/api/propositions/', '/api/sync/')):
            if response.status_code >= 500:
                response = JsonResponse({'code': 'internal_error', 'detail': 'Analyse indisponible.'}, status=response.status_code)
            if response.status_code == 403:
                response = JsonResponse({'code': 'forbidden', 'detail': 'Requête refusée. Vérifiez votre session et la protection CSRF.'}, status=403)
            response['Referrer-Policy'] = 'no-referrer'
            add_never_cache_headers(response)
        return response
