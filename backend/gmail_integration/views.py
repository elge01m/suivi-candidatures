import secrets
import time
from functools import wraps

from django.http import HttpResponseRedirect, JsonResponse
from django.utils.crypto import constant_time_compare
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_variables
from django.views.decorators.http import require_GET, require_POST

from .crypto import OAuthConfigurationError, OAuthFailure
from .services import build_flow, exchange_code
from .services import frontend_destination


PENDING_KEY = 'gmail_oauth_pending'
STATE_LIFETIME = 600


def oauth_endpoint(view):
    @wraps(view)
    @sensitive_variables()
    def wrapped(request, *args, **kwargs):
        try:
            if not request.user.is_authenticated:
                response = JsonResponse({'detail': 'Authentification requise.'}, status=401)
            else:
                response = view(request, *args, **kwargs)
        except OAuthConfigurationError:
            response = JsonResponse({'detail': 'La connexion Gmail n’est pas encore configurée.'}, status=503)
        except OAuthFailure:
            response = JsonResponse({'detail': 'Autorisation Gmail incomplète. Recommencez la connexion.'}, status=400)
        except Exception:
            # Deliberately do not log the exception, response body or local variables.
            # This also prevents Django DEBUG pages from exposing OAuth credentials.
            response = JsonResponse({'detail': 'La connexion Gmail a échoué. Réessayez ultérieurement.'}, status=503)
        response['Referrer-Policy'] = 'no-referrer'
        return response
    return never_cache(wrapped)


@oauth_endpoint
@require_POST
@sensitive_variables()
def connect(request):
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    flow = build_flow(state, verifier)
    url, returned_state = flow.authorization_url(
        access_type='offline', prompt='consent', include_granted_scopes='false',
    )
    if not constant_time_compare(state, returned_state):
        raise OAuthFailure
    request.session[PENDING_KEY] = {
        'state': state, 'verifier': verifier,
        'user_id': request.user.pk, 'created_at': time.time(),
    }
    return HttpResponseRedirect(url)


@oauth_endpoint
@require_GET
@sensitive_variables()
def callback(request):
    pending = request.session.get(PENDING_KEY)
    states = request.GET.getlist('state')
    if not isinstance(pending, dict) or len(states) != 1 or not states[0] or \
            not constant_time_compare(states[0], pending.get('state', '')):
        raise OAuthFailure
    # Consume the transaction before the network request, including refused consent.
    request.session.pop(PENDING_KEY, None)
    # SessionMiddleware intentionally skips saving on 5xx responses. Persist the
    # consumption now so an exchange/network failure cannot revive this state.
    request.session.save()
    age = time.time() - pending['created_at']
    if pending.get('user_id') != request.user.pk or not 0 <= age <= STATE_LIFETIME:
        raise OAuthFailure
    codes = request.GET.getlist('code')
    if 'error' in request.GET or len(codes) != 1 or not codes[0]:
        raise OAuthFailure
    destination = frontend_destination()
    exchange_code(request.user, codes[0], states[0], pending['verifier'])
    # Fixed clean destination: never forward code, state, tokens or arbitrary next URLs.
    return HttpResponseRedirect(destination)
