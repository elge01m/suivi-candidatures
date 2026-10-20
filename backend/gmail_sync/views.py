import json
from functools import wraps

from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .errors import SyncProblem
from .models import SyncRun
from .services import configuration_data, configure, request_initial_import, request_sync, retry_import, run_data


def endpoint(view):
    @wraps(view)
    @sensitive_variables()
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated or not request.user.is_active:
            return JsonResponse({'code':'authentication_required', 'detail':'Authentification requise.'}, status=401)
        try:
            return view(request, *args, **kwargs)
        except SyncProblem as error:
            return JsonResponse({'code':error.code, 'detail':error.message}, status=error.status)
        except Exception:
            return JsonResponse({'code':'sync_unavailable', 'detail':'Synchronisation indisponible.'}, status=503)
    return never_cache(wrapped)


def body(request):
    try:
        if request.content_type != 'application/json' or len(request.body) > 2000:
            raise ValueError
        result = json.loads(request.body)
        if not isinstance(result, dict):
            raise ValueError
        return result
    except (ValueError, UnicodeError):
        raise SyncProblem('invalid_request') from None


@endpoint
@require_http_methods(['GET', 'PATCH'])
def configuration(request):
    if request.method == 'PATCH':
        configure(request.user, body(request))
    return JsonResponse(configuration_data(request.user))


@endpoint
@require_POST
def enqueue(request):
    if body(request):
        raise SyncProblem('invalid_request')
    return JsonResponse(run_data(request_sync(request.user)), status=202)


@endpoint
@require_POST
def initial_import(request):
    return JsonResponse(run_data(request_initial_import(request.user, body(request))), status=202)


@endpoint
@require_POST
def retry(request, import_id):
    data = body(request)
    if data != {'confirmer': True} or data.get('confirmer') is not True:
        raise SyncProblem('invalid_request')
    return JsonResponse(run_data(retry_import(request.user, import_id, confirm=True)), status=202)


@endpoint
@require_GET
def status(request, run_id):
    run = SyncRun.objects.select_related('import_job').filter(pk=run_id, configuration__connexion__utilisateur=request.user).first()
    if run is None:
        raise SyncProblem('not_found')
    return JsonResponse(run_data(run))
