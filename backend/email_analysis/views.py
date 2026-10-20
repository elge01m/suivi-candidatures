import json
from functools import wraps

from django.http import JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from .errors import AnalysisProblem
from .models import CandidatureProposal, EmailAnalysis
from .services import (accept_proposal, analysis_for, analysis_retry_allowed, analyze_message, correct_proposal,
                       proposal_data, reject_proposal)


def endpoint(view):
    @wraps(view)
    @sensitive_variables()
    def wrapped(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({'code': 'authentication_required', 'detail': 'Authentification requise.'}, status=401)
        try:
            return view(request, *args, **kwargs)
        except AnalysisProblem as error:
            return JsonResponse({'code': error.code, 'detail': error.public_message}, status=error.status)
        except Exception:
            return JsonResponse({'code': 'internal_error', 'detail': 'Analyse indisponible.'}, status=503)
    return never_cache(wrapped)


def body(request, allowed, required):
    try:
        if request.content_type != 'application/json' or len(request.body) > 16000:
            raise ValueError
        data = json.loads(request.body)
        if not isinstance(data, dict) or set(data) - set(allowed) or not set(required) <= set(data):
            raise ValueError
        return data
    except (ValueError, UnicodeError):
        raise AnalysisProblem('invalid_request') from None


def analysis_data(job):
    proposal_id = CandidatureProposal.objects.filter(analysis=job).values_list('pk', flat=True).first()
    return {'id': str(job.pk), 'status': job.status, 'attempts': job.attempts, 'error_code': job.error_code,
            'can_retry':analysis_retry_allowed(job), 'message_reference':job.message_id,
            'retry_after': job.retry_after.isoformat() if job.retry_after else None,
            'input_tokens': job.input_tokens, 'output_tokens': job.output_tokens,
            'proposition_id': str(proposal_id) if proposal_id else None}


@endpoint
@require_http_methods(['GET', 'POST'])
def analyses(request):
    if request.method == 'GET':
        rows = EmailAnalysis.objects.filter(message__connexion__utilisateur=request.user).order_by('-created_at')[:100]
        return JsonResponse({'results': [analysis_data(row) for row in rows]})
    data = body(request, ['message_id', 'consentement'], ['message_id', 'consentement'])
    job = analyze_message(request.user, data['message_id'], consent=data['consentement'])
    return JsonResponse(analysis_data(job))


@endpoint
@require_GET
def analysis_detail(request, analysis_id):
    return JsonResponse(analysis_data(analysis_for(request.user, analysis_id)))


@endpoint
@require_POST
def retry_analysis(request, analysis_id):
    data = body(request, ['consentement'], ['consentement'])
    job = analysis_for(request.user, analysis_id)
    if not analysis_retry_allowed(job):
        raise AnalysisProblem('retry_unavailable')
    return JsonResponse(analysis_data(analyze_message(request.user, job.message_id, consent=data['consentement'], retry=True)))


@endpoint
@require_GET
def proposals(request):
    # List metadata only; sensitive proposal details require a separate scoped GET.
    rows = CandidatureProposal.objects.filter(utilisateur=request.user, analysis__message__connexion__utilisateur=request.user).order_by('-created_at')[:100]
    return JsonResponse({'results': [{'id': str(row.pk), 'status': row.status, 'revision': row.revision} for row in rows]})


@endpoint
@require_http_methods(['GET', 'PATCH'])
def proposal_detail(request, proposal_id):
    if request.method == 'PATCH':
        data = body(request, ['revision', 'proposition'], ['revision', 'proposition'])
        correct_proposal(request.user, proposal_id, data['proposition'], data['revision'])
    return JsonResponse(proposal_data(request.user, proposal_id))


@endpoint
@require_POST
def accept(request, proposal_id):
    data = body(request, ['revision', 'confirmer'], ['revision', 'confirmer'])
    accept_proposal(request.user, proposal_id, data['revision'], confirm=data['confirmer'])
    return JsonResponse(proposal_data(request.user, proposal_id))


@endpoint
@require_POST
def reject(request, proposal_id):
    data = body(request, ['revision', 'confirmer'], ['revision', 'confirmer'])
    reject_proposal(request.user, proposal_id, data['revision'], confirm=data['confirmer'])
    return JsonResponse(proposal_data(request.user, proposal_id))
