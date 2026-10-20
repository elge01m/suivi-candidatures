import json
from functools import wraps

from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .import_errors import ImportProblem
from .import_service import continue_import, get_import, prepare_import, purge_messages


def safe_json(body, status=200):
    response = JsonResponse(body, status=status)
    # Only this module's fixed-schema responses bypass the generic privacy body.
    response.gmail_safe_response = True
    return response


def import_endpoint(view):
    @wraps(view)
    @sensitive_variables()
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return safe_json({'code': 'authentication_required', 'detail': 'Authentification requise.'}, 401)
        try:
            return view(request, *args, **kwargs)
        except ImportProblem as error:
            return safe_json({'code': error.code, 'detail': error.public_message}, error.status)
        except Exception:
            return safe_json({'code': 'internal_error', 'detail': 'L’import est indisponible.'}, 503)
    return never_cache(wrapped)


def json_body(request):
    if request.content_type != 'application/json':
        raise ImportProblem('invalid_request')
    try:
        body = json.loads(request.body)
    except (ValueError, UnicodeError):
        raise ImportProblem('invalid_request') from None
    if not isinstance(body, dict):
        raise ImportProblem('invalid_request')
    return body


def progress(job):
    return {
        'id': str(job.pk), 'date_debut': job.date_debut.isoformat(), 'date_fin': job.date_fin.isoformat(),
        'fuseau': job.fuseau, 'status': job.status,
        'processed': job.processed, 'imported': job.imported, 'duplicates': job.duplicates,
        'skipped': job.skipped, 'incomplete': job.incomplete,
        'error_code': job.error_code,
        'retry_after': job.retry_after.isoformat() if job.retry_after else None,
    }


@import_endpoint
@require_http_methods(['GET', 'POST'])
def create_import(request):
    if request.method == 'GET':
        from gmail_sync.services import import_retry_allowed
        from .models import GmailImport
        rows = GmailImport.objects.filter(connexion__utilisateur=request.user).order_by('-date_creation')[:100]
        return safe_json({'results':[progress(job) | {'can_retry':import_retry_allowed(job)} for job in rows]})
    data = json_body(request)
    if set(data) - {'date_debut', 'date_fin', 'fuseau'}:
        raise ImportProblem('invalid_request')
    job = prepare_import(request.user, data.get('date_debut'), data.get('date_fin'),
                         data.get('fuseau', 'America/Toronto'))
    return safe_json(progress(job), 201)


@import_endpoint
@require_POST
def next_batch(request, import_id):
    if request.content_type == 'application/json':
        if json_body(request):
            raise ImportProblem('invalid_request')
    elif request.content_type in ('multipart/form-data', 'application/x-www-form-urlencoded'):
        if request.POST or request.FILES:
            raise ImportProblem('invalid_request')
    elif request.body:
        raise ImportProblem('invalid_request')
    return safe_json(progress(continue_import(request.user, import_id)))


@import_endpoint
@require_GET
def import_status(request, import_id):
    return safe_json(progress(get_import(request.user, import_id)))


@import_endpoint
@require_http_methods(['DELETE'])
def delete_messages(request):
    data = json_body(request)
    if data != {'confirm': True} or data.get('confirm') is not True:
        raise ImportProblem('confirmation_required')
    return safe_json({'deleted': purge_messages(request.user)})
